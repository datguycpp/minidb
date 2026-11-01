/* M38: the single-table aggregate loop, in C.
 *
 * The Python path this replaces builds a dict per row (`build_env`: two lowercased keys per
 * column per row) and then walks an AST per aggregate item. For `SELECT COUNT(*), SUM(v)`, that
 * is the whole cost of the query. This module takes the row tuples and the column indices
 * directly, so the loop is a loop.
 *
 * Parity is the acceptance, not speed: the arithmetic is Python's own (PyNumber_Add,
 * PyNumber_TrueDivide, PyObject_RichCompareBool), so ints stay ints, floats promote, NULLs are
 * skipped, an empty aggregate is NULL, and MIN/MAX keep the first minimal element exactly as
 * `min()`/`max()` do. Anything ambiguous — a non-numeric value under SUM, a non-tuple row — is
 * reported as unsupported and the caller runs the Python path, which is the one every corpus
 * case already pins.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

enum {
    F_COUNT_STAR = 0,
    F_COUNT_COL  = 1,
    F_SUM        = 2,
    F_AVG        = 3,
    F_MIN        = 4,
    F_MAX        = 5
};

typedef struct {
    PyObject *acc;        /* owned reference; NULL until the first non-NULL value */
    long long count;
} Slot;

typedef struct {
    PyObject **accs;      /* owned, one per spec */
    long long *counts;
} Group;

static void
group_clear(Group *group, Py_ssize_t nspecs)
{
    if (group->accs != NULL) {
        for (Py_ssize_t i = 0; i < nspecs; i++) {
            Py_XDECREF(group->accs[i]);
        }
        PyMem_Free(group->accs);
    }
    PyMem_Free(group->counts);
    group->accs = NULL;
    group->counts = NULL;
}

static int
group_init(Group *group, Py_ssize_t nspecs)
{
    group->accs = PyMem_Calloc((size_t)nspecs, sizeof(PyObject *));
    group->counts = PyMem_Calloc((size_t)nspecs, sizeof(long long));
    return (group->accs != NULL && group->counts != NULL) ? 0 : -1;
}

/* Returns 1 on success, 0 when the row's values cannot be handled here, -1 on a real error. */
static int
accumulate(Group *group, PyObject *row, Py_ssize_t nspecs,
           const int *funcs, const long *columns)
{
    for (Py_ssize_t i = 0; i < nspecs; i++) {
        PyObject *value = NULL;

        if (funcs[i] == F_COUNT_STAR) {
            group->counts[i] += 1;
            continue;
        }
        value = PyTuple_GET_ITEM(row, columns[i]);
        if (value == Py_None) {
            continue;                      /* SQL: NULLs are not aggregated */
        }
        switch (funcs[i]) {
        case F_COUNT_COL:
            group->counts[i] += 1;
            break;
        case F_SUM:
        case F_AVG: {
            PyObject *sum;
            if (!PyLong_Check(value) && !PyFloat_Check(value)) {
                return 0;                  /* strings under SUM: let Python raise its own error */
            }
            if (group->accs[i] == NULL) {
                group->accs[i] = PyLong_FromLong(0);
                if (group->accs[i] == NULL) {
                    return -1;
                }
            }
            sum = PyNumber_Add(group->accs[i], value);
            if (sum == NULL) {
                return -1;
            }
            Py_SETREF(group->accs[i], sum);
            group->counts[i] += 1;
            break;
        }
        case F_MIN:
        case F_MAX: {
            int replace;
            if (group->accs[i] == NULL) {
                group->accs[i] = Py_NewRef(value);
                group->counts[i] += 1;
                break;
            }
            replace = PyObject_RichCompareBool(value, group->accs[i],
                                               funcs[i] == F_MIN ? Py_LT : Py_GT);
            if (replace < 0) {
                return -1;
            }
            if (replace) {
                Py_SETREF(group->accs[i], Py_NewRef(value));
            }
            group->counts[i] += 1;
            break;
        }
        default:
            return 0;
        }
    }
    return 1;
}

static PyObject *
finish_group(Group *group, Py_ssize_t nspecs, const int *funcs)
{
    PyObject *out = PyTuple_New(nspecs);
    if (out == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < nspecs; i++) {
        PyObject *value = NULL;
        if (funcs[i] == F_COUNT_STAR || funcs[i] == F_COUNT_COL) {
            value = PyLong_FromLongLong(group->counts[i]);
        } else if (group->counts[i] == 0 || group->accs[i] == NULL) {
            value = Py_NewRef(Py_None);
        } else if (funcs[i] == F_SUM || funcs[i] == F_MIN || funcs[i] == F_MAX) {
            value = Py_NewRef(group->accs[i]);
        } else if (funcs[i] == F_AVG) {
            PyObject *count = PyLong_FromLongLong(group->counts[i]);
            if (count != NULL) {
                value = PyNumber_TrueDivide(group->accs[i], count);
                Py_DECREF(count);
            }
        }
        if (value == NULL) {
            Py_DECREF(out);
            return NULL;
        }
        PyTuple_SET_ITEM(out, i, value);
    }
    return out;
}

static PyObject *
group_aggregate(PyObject *module, PyObject *args)
{
    PyObject *rows;
    PyObject *specs;
    int key_index;

    (void)module;
    if (!PyArg_ParseTuple(args, "OiO", &rows, &key_index, &specs)) {
        return NULL;
    }
    if (!PyList_Check(specs)) {
        Py_RETURN_NONE;
    }
    Py_ssize_t nspecs = PyList_GET_SIZE(specs);
    int *funcs = PyMem_Calloc((size_t)(nspecs > 0 ? nspecs : 1), sizeof(int));
    long *columns = PyMem_Calloc((size_t)(nspecs > 0 ? nspecs : 1), sizeof(long));
    if (funcs == NULL || columns == NULL) {
        PyMem_Free(funcs);
        PyMem_Free(columns);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < nspecs; i++) {
        PyObject *spec = PyList_GET_ITEM(specs, i);
        if (!PyTuple_Check(spec) || PyTuple_GET_SIZE(spec) != 2) {
            PyMem_Free(funcs);
            PyMem_Free(columns);
            Py_RETURN_NONE;
        }
        funcs[i] = (int)PyLong_AsLong(PyTuple_GET_ITEM(spec, 0));
        columns[i] = (long)PyLong_AsLong(PyTuple_GET_ITEM(spec, 1));
        if (PyErr_Occurred()) {
            PyMem_Free(funcs);
            PyMem_Free(columns);
            return NULL;
        }
    }

    PyObject *sequence = PySequence_Fast(rows, "rows must be a sequence");
    if (sequence == NULL) {
        PyMem_Free(funcs);
        PyMem_Free(columns);
        return NULL;
    }
    Py_ssize_t nrows = PySequence_Fast_GET_SIZE(sequence);

    PyObject *keys = PyList_New(0);
    PyObject *index = PyDict_New();
    Group *groups = PyMem_Calloc((size_t)(nrows > 0 ? nrows : 1), sizeof(Group));
    Py_ssize_t ngroups = 0;
    PyObject *result = NULL;
    if (keys == NULL || index == NULL || groups == NULL) {
        PyErr_NoMemory();
        goto done;
    }

    /* A query with no GROUP BY has exactly one group, even when the table is empty. */
    if (key_index < 0) {
        if (group_init(&groups[0], nspecs) != 0) {
            PyErr_NoMemory();
            goto done;
        }
        ngroups = 1;
        if (PyList_Append(keys, Py_None) != 0) {
            goto done;
        }
        PyObject *zero = PyLong_FromLong(0);
        if (zero == NULL || PyDict_SetItem(index, Py_None, zero) != 0) {
            Py_XDECREF(zero);
            goto done;
        }
        Py_DECREF(zero);
    }

    for (Py_ssize_t r = 0; r < nrows; r++) {
        PyObject *row = PySequence_Fast_GET_ITEM(sequence, r);
        Py_ssize_t group = 0;
        int status;

        if (!PyTuple_Check(row)) {
            goto unsupported;
        }
        if (key_index >= 0) {
            PyObject *key = PyTuple_GET_ITEM(row, key_index);
            PyObject *found = PyDict_GetItemWithError(index, key);
            if (found == NULL) {
                if (PyErr_Occurred()) {
                    goto done;
                }
                if (group_init(&groups[ngroups], nspecs) != 0) {
                    PyErr_NoMemory();
                    goto done;
                }
                if (PyList_Append(keys, key) != 0) {
                    goto done;
                }
                PyObject *number = PyLong_FromSsize_t(ngroups);
                if (number == NULL || PyDict_SetItem(index, key, number) != 0) {
                    Py_XDECREF(number);
                    goto done;
                }
                Py_DECREF(number);
                group = ngroups;
                ngroups += 1;
            } else {
                group = (Py_ssize_t)PyLong_AsSsize_t(found);
                if (group < 0 || group >= ngroups) {
                    goto unsupported;
                }
            }
        }
        status = accumulate(&groups[group], row, nspecs, funcs, columns);
        if (status == 0) {
            goto unsupported;
        }
        if (status < 0) {
            goto done;
        }
    }

    result = PyDict_New();
    if (result == NULL) {
        goto done;
    }
    for (Py_ssize_t g = 0; g < ngroups; g++) {
        PyObject *key = PyList_GET_ITEM(keys, g);
        PyObject *value = finish_group(&groups[g], nspecs, funcs);
        if (value == NULL) {
            Py_CLEAR(result);
            goto done;
        }
        if (PyDict_SetItem(result, key, value) != 0) {
            Py_DECREF(value);
            Py_CLEAR(result);
            goto done;
        }
        Py_DECREF(value);
    }
    goto done;

unsupported:
    Py_CLEAR(result);
    result = Py_NewRef(Py_None);      /* contract: None means "run the Python path" */

done:
    for (Py_ssize_t g = 0; g < ngroups; g++) {
        group_clear(&groups[g], nspecs);
    }
    PyMem_Free(groups);
    Py_XDECREF(keys);
    Py_XDECREF(index);
    Py_DECREF(sequence);
    PyMem_Free(funcs);
    PyMem_Free(columns);
    return result;
}

static PyMethodDef methods[] = {
    {"group_aggregate", group_aggregate, METH_VARARGS,
     "group_aggregate(rows, key_index, specs) -> {key: (value, ...)} | None when unsupported"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_native", "M38 C accelerator: single-table aggregate loop.", -1, methods,
    NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC
PyInit__native(void)
{
    return PyModule_Create(&module);
}
