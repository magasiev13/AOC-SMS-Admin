(() => {
  /*
   * How to add a sortable column:
   * 1) Add data-sortable="client" (or "server") on the table.
   * 2) Wrap the header label in a button with data-sort-key and set data-sort-type on the <th>.
   * 3) (Optional) Add data-sort-value to <td> cells to override the displayed text.
   */
  const collator = new Intl.Collator(undefined, { sensitivity: 'base' });

  const normalizeNumber = (value) => {
    if (!value) {
      return null;
    }
    const cleaned = value.replace(/[^\d.-]/g, '');
    if (!cleaned) {
      return null;
    }
    const parsed = Number(cleaned);
    return Number.isNaN(parsed) ? null : parsed;
  };

  const normalizeDate = (value) => {
    if (!value) {
      return null;
    }
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  };

  const normalizePhone = (value) => {
    if (!value) {
      return null;
    }
    const digits = value.replace(/\D/g, '');
    if (!digits) {
      return null;
    }
    const parsed = Number(digits);
    return Number.isNaN(parsed) ? null : parsed;
  };

  const normalizeValue = (rawValue, type) => {
    const value = (rawValue ?? '').toString().trim();
    if (!value) {
      return { value: null, isEmpty: true };
    }

    switch (type) {
      case 'number': {
        const parsed = normalizeNumber(value);
        return { value: parsed, isEmpty: parsed === null };
      }
      case 'date': {
        const parsed = normalizeDate(value);
        return { value: parsed, isEmpty: parsed === null };
      }
      case 'phone': {
        const parsed = normalizePhone(value);
        return { value: parsed, isEmpty: parsed === null };
      }
      case 'enum':
      case 'string':
      default:
        return { value, isEmpty: false };
    }
  };

  const compareValues = (aRaw, bRaw, type, direction) => {
    const a = normalizeValue(aRaw, type);
    const b = normalizeValue(bRaw, type);

    if (a.isEmpty && b.isEmpty) {
      return 0;
    }
    if (a.isEmpty) {
      return direction === 'asc' ? 1 : -1;
    }
    if (b.isEmpty) {
      return direction === 'asc' ? -1 : 1;
    }

    if (type === 'number' || type === 'date' || type === 'phone') {
      return a.value - b.value;
    }

    return collator.compare(a.value, b.value);
  };

  const getCellValue = (cell) => {
    if (!cell) {
      return '';
    }
    if (cell.dataset.sortValue !== undefined) {
      return cell.dataset.sortValue;
    }
    return cell.textContent.trim();
  };

  const setSortIndicators = (table, activeTh, direction) => {
    const headers = table.querySelectorAll('thead th');
    headers.forEach((th) => {
      if (!th.dataset.sortType) {
        return;
      }
      th.setAttribute('aria-sort', 'none');
      const button = th.querySelector('[data-sort-key]');
      if (button) {
        button.classList.remove('is-sorted-asc', 'is-sorted-desc');
      }
    });

    if (activeTh) {
      activeTh.setAttribute('aria-sort', direction === 'asc' ? 'ascending' : 'descending');
      const button = activeTh.querySelector('[data-sort-key]');
      if (button) {
        button.classList.toggle('is-sorted-asc', direction === 'asc');
        button.classList.toggle('is-sorted-desc', direction === 'desc');
      }
    }
  };

  const applyClientSort = (table, th, direction) => {
    const tbody = table.querySelector('tbody.table-data') || table.tBodies[0];
    if (!tbody) {
      return;
    }
    const rows = Array.from(tbody.querySelectorAll('tr'));
    if (rows.length <= 1) {
      return;
    }

    rows.forEach((row, index) => {
      if (!row.dataset.sortIndex) {
        row.dataset.sortIndex = index.toString();
      }
    });

    const columnIndex = th.cellIndex;
    const type = th.dataset.sortType || 'string';

    const sorted = rows
      .map((row) => ({
        row,
        index: Number(row.dataset.sortIndex),
        value: getCellValue(row.cells[columnIndex]),
      }))
      .sort((a, b) => {
        const compare = compareValues(a.value, b.value, type, direction);
        if (compare !== 0) {
          return direction === 'asc' ? compare : -compare;
        }
        return a.index - b.index;
      });

    const fragment = document.createDocumentFragment();
    sorted.forEach((item) => fragment.appendChild(item.row));
    tbody.appendChild(fragment);
  };

  const applyServerSort = (table, th, direction) => {
    const sortKey = th.querySelector('[data-sort-key]')?.dataset.sortKey;
    if (!sortKey) {
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set('sort', sortKey);
    url.searchParams.set('dir', direction);
    url.searchParams.set('page', '1');
    window.location.assign(url.toString());
  };

  const getSortState = (table) => {
    const url = new URL(window.location.href);
    const sortKey = url.searchParams.get('sort') || table.dataset.sortDefault || '';
    const sortDir = url.searchParams.get('dir') || table.dataset.sortDefaultDir || 'asc';
    return {
      sortKey,
      sortDir: sortDir === 'desc' ? 'desc' : 'asc',
    };
  };

  const initSortableTable = (table) => {
    const mode = table.dataset.sortable || 'client';
    const headers = Array.from(table.querySelectorAll('thead th'));

    headers.forEach((th) => {
      if (!th.dataset.sortType) {
        return;
      }
      th.setAttribute('aria-sort', 'none');
      const button = th.querySelector('[data-sort-key]');
      if (!button) {
        return;
      }

      button.addEventListener('click', () => {
        const key = button.dataset.sortKey || th.cellIndex.toString();
        const currentKey = table.dataset.sortKey;
        const currentDir = table.dataset.sortDir || 'asc';
        const nextDir = currentKey === key && currentDir === 'asc' ? 'desc' : 'asc';

        table.dataset.sortKey = key;
        table.dataset.sortDir = nextDir;
        setSortIndicators(table, th, nextDir);

        if (mode === 'server') {
          applyServerSort(table, th, nextDir);
        } else {
          applyClientSort(table, th, nextDir);
        }
      });
    });

    if (mode === 'server') {
      const { sortKey, sortDir } = getSortState(table);
      const activeTh = headers.find((th) => th.querySelector('[data-sort-key]')?.dataset.sortKey === sortKey);
      if (activeTh) {
        table.dataset.sortKey = sortKey;
        table.dataset.sortDir = sortDir;
        setSortIndicators(table, activeTh, sortDir);
      }
    }
  };

  const init = () => {
    document.querySelectorAll('table[data-sortable]').forEach((table) => {
      initSortableTable(table);
    });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
