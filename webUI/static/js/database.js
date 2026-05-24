/**
 * NachoBot WebUI — Database Module
 * Browse and manage SQLite database tables with per-column filtering.
 */

const DatabaseModule = (() => {
    let tables = [];
    let activeTable = null;
    let currentPage = 1;
    let currentSortBy = 'id';
    let currentSortOrder = 'desc';
    /** @type {Record<string, string>} column_name -> filter_value */
    let activeFilters = {};
    /** Cache of distinct values per column: { table: { col: string[] } } */
    let columnValuesCache = {};

    function init() {
        document.getElementById('db-filter-clear-all').addEventListener('click', () => {
            activeFilters = {};
            currentPage = 1;
            renderFilterBar();
            loadTableData();
        });
    }

    async function refresh() {
        try {
            tables = await apiGet('/api/db/tables');
            renderTableList();
            renderStats();
        } catch (e) {
            toast('加载数据库信息失败', 'error');
        }
    }

    async function renderStats() {
        try {
            const stats = await apiGet('/api/db/stats');
            const el = document.getElementById('db-stats');
            if (!stats.exists) {
                el.innerHTML = '<div class="db-stats-bar"><span class="stat-item">⚠️ 数据库文件未找到</span></div>';
                return;
            }
            el.innerHTML = `
                <div class="db-stats-bar">
                    <span class="stat-item">💾 数据库大小：<strong>${stats.size_mb} MB</strong></span>
                    <span class="stat-item">📊 数据表：<strong>${stats.tables.length}</strong> 个</span>
                    <span class="stat-item">📝 总记录：<strong>${stats.tables.reduce((s, t) => s + t.rows, 0).toLocaleString()}</strong> 条</span>
                </div>
            `;
        } catch (e) { /* ignore */ }
    }

    function renderTableList() {
        const container = document.getElementById('db-table-list');
        container.innerHTML = '';

        for (const t of tables) {
            const item = document.createElement('div');
            item.className = `db-table-item ${activeTable === t.name ? 'active' : ''}`;
            item.innerHTML = `
                <span class="db-table-name">${escapeHtml(t.label)}</span>
                <span class="db-table-count">${t.rows.toLocaleString()}</span>
            `;
            item.addEventListener('click', () => {
                activeTable = t.name;
                currentPage = 1;
                activeFilters = {};
                currentSortBy = 'id';
                currentSortOrder = 'desc';
                renderTableList();
                renderFilterBar();
                loadTableData();
            });
            container.appendChild(item);
        }
    }

    // ---- Filter bar rendering ----

    function renderFilterBar() {
        const bar = document.getElementById('db-filter-bar');
        const clearBtn = document.getElementById('db-filter-clear-all');
        const keys = Object.keys(activeFilters);

        if (keys.length === 0) {
            bar.innerHTML = '<span class="db-filter-hint">🔍 点击表头的筛选图标按列筛选</span>';
            clearBtn.style.display = 'none';
            return;
        }

        clearBtn.style.display = 'inline-flex';
        let html = '';
        for (const col of keys) {
            const val = activeFilters[col];
            html += `<span class="db-filter-tag">
                <span class="db-filter-tag-col">${escapeHtml(col)}</span>
                <span class="db-filter-tag-eq">=</span>
                <span class="db-filter-tag-val">${escapeHtml(val)}</span>
                <button class="db-filter-tag-remove" data-col="${escapeHtml(col)}" title="移除此筛选">✕</button>
            </span>`;
        }
        bar.innerHTML = html;

        // Bind remove buttons
        bar.querySelectorAll('.db-filter-tag-remove').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const col = btn.dataset.col;
                delete activeFilters[col];
                currentPage = 1;
                renderFilterBar();
                loadTableData();
            });
        });
    }

    // ---- Load column distinct values ----

    async function getColumnValues(table, column) {
        const cacheKey = `${table}.${column}`;
        if (!columnValuesCache[cacheKey]) {
            try {
                columnValuesCache[cacheKey] = await apiGet(`/api/db/tables/${table}/columns/${encodeURIComponent(column)}/values`);
            } catch (e) {
                columnValuesCache[cacheKey] = [];
            }
        }
        return columnValuesCache[cacheKey];
    }

    // ---- Filter dropdown ----

    async function openFilterDropdown(th, colName) {
        // Close any existing dropdown
        closeFilterDropdown();

        const values = await getColumnValues(activeTable, colName);

        // Create dropdown
        const dropdown = document.createElement('div');
        dropdown.className = 'db-filter-dropdown';
        dropdown.id = 'db-filter-dropdown-active';

        // Search input for filtering options
        let html = `<div class="db-filter-dd-search">
            <input type="text" class="db-filter-dd-input" placeholder="搜索值..." id="dd-search-input">
        </div>`;
        html += '<div class="db-filter-dd-options" id="dd-options">';

        if (values.length === 0) {
            html += '<div class="db-filter-dd-empty">无可用值</div>';
        } else {
            for (const v of values) {
                const isActive = activeFilters[colName] === v;
                html += `<div class="db-filter-dd-option ${isActive ? 'active' : ''}" data-val="${escapeHtml(v)}">
                    ${isActive ? '<span class="dd-check">✓</span>' : '<span class="dd-check-empty"></span>'}
                    <span class="dd-option-text">${escapeHtml(v)}</span>
                </div>`;
            }
        }
        html += '</div>';

        // Clear filter for this column
        if (activeFilters[colName] !== undefined) {
            html += `<div class="db-filter-dd-footer">
                <button class="db-filter-dd-clear" id="dd-clear-col">清除此列筛选</button>
            </div>`;
        }

        dropdown.innerHTML = html;

        // Position dropdown below the th
        const rect = th.getBoundingClientRect();
        const scrollContainer = document.querySelector('.db-table-scroll');
        const scrollRect = scrollContainer ? scrollContainer.getBoundingClientRect() : { left: 0, top: 0 };

        dropdown.style.position = 'fixed';
        dropdown.style.left = `${rect.left}px`;
        dropdown.style.top = `${rect.bottom + 4}px`;

        document.body.appendChild(dropdown);

        // Ensure dropdown doesn't overflow the viewport
        requestAnimationFrame(() => {
            const ddRect = dropdown.getBoundingClientRect();
            if (ddRect.right > window.innerWidth) {
                dropdown.style.left = `${window.innerWidth - ddRect.width - 8}px`;
            }
            if (ddRect.bottom > window.innerHeight) {
                dropdown.style.top = `${rect.top - ddRect.height - 4}px`;
            }
        });

        // Bind option clicks
        dropdown.querySelectorAll('.db-filter-dd-option').forEach(opt => {
            opt.addEventListener('click', () => {
                const val = opt.dataset.val;
                if (activeFilters[colName] === val) {
                    delete activeFilters[colName];
                } else {
                    activeFilters[colName] = val;
                }
                currentPage = 1;
                closeFilterDropdown();
                renderFilterBar();
                loadTableData();
            });
        });

        // Bind clear button
        const clearBtn = dropdown.querySelector('#dd-clear-col');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                delete activeFilters[colName];
                currentPage = 1;
                closeFilterDropdown();
                renderFilterBar();
                loadTableData();
            });
        }

        // Bind search
        const searchInput = dropdown.querySelector('#dd-search-input');
        if (searchInput) {
            searchInput.focus();
            searchInput.addEventListener('input', () => {
                const query = searchInput.value.toLowerCase();
                dropdown.querySelectorAll('.db-filter-dd-option').forEach(opt => {
                    const text = opt.querySelector('.dd-option-text').textContent.toLowerCase();
                    opt.style.display = text.includes(query) ? '' : 'none';
                });
            });
        }

        // Close on outside click
        setTimeout(() => {
            document.addEventListener('click', onOutsideClick);
        }, 10);
    }

    function onOutsideClick(e) {
        const dd = document.getElementById('db-filter-dropdown-active');
        if (dd && !dd.contains(e.target) && !e.target.classList.contains('db-th-filter-btn')) {
            closeFilterDropdown();
        }
    }

    function closeFilterDropdown() {
        const dd = document.getElementById('db-filter-dropdown-active');
        if (dd) dd.remove();
        document.removeEventListener('click', onOutsideClick);
    }

    // ---- Data loading ----

    async function loadTableData() {
        if (!activeTable) return;

        const toolbar = document.getElementById('db-toolbar');
        toolbar.style.display = 'flex';

        try {
            const params = new URLSearchParams({
                page: currentPage,
                size: 50,
                sort_by: currentSortBy,
                sort_order: currentSortOrder,
            });
            if (Object.keys(activeFilters).length > 0) {
                params.set('filters', JSON.stringify(activeFilters));
            }
            const result = await apiGet(`/api/db/tables/${activeTable}?${params}`);
            renderTable(result);
            renderPagination(result);

            const filterCount = Object.keys(activeFilters).length;
            const filterInfo = filterCount > 0 ? ` | ${filterCount} 个筛选` : '';
            document.getElementById('db-table-info').textContent =
                `${result.label} — 共 ${result.total.toLocaleString()} 条${result.editable ? ' (可编辑)' : ' (只读)'}${filterInfo}`;
        } catch (e) {
            toast('加载表数据失败: ' + e.message, 'error');
        }
    }

    function renderTable(result) {
        const container = document.getElementById('db-table-container');
        if (result.data.length === 0) {
            container.innerHTML = '<div class="editor-placeholder"><p>无数据</p></div>';
            return;
        }

        const cols = result.columns;
        let html = '<div class="db-table-scroll"><table class="db-table"><thead><tr>';

        // Headers with filter icon
        for (const col of cols) {
            const isSort = col.name === currentSortBy;
            const arrow = isSort ? (currentSortOrder === 'asc' ? ' ▲' : ' ▼') : '';
            const hasFilter = activeFilters[col.name] !== undefined;
            html += `<th class="db-th ${isSort ? 'sorted' : ''} ${hasFilter ? 'filtered' : ''}" data-col="${col.name}">
                <span class="db-th-content">
                    <span class="db-th-label">${escapeHtml(col.name)}${arrow}</span>
                    <button class="db-th-filter-btn ${hasFilter ? 'active' : ''}" data-col="${col.name}" title="筛选 ${col.name}">
                        <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M1.5 1.5h13l-5 6v5l-3 2v-7z"/></svg>
                    </button>
                </span>
            </th>`;
        }
        if (result.editable) {
            html += '<th class="db-th db-th-actions">操作</th>';
        }
        html += '</tr></thead><tbody>';

        // Rows
        for (const row of result.data) {
            html += '<tr>';
            for (const col of cols) {
                const val = row[col.name];
                const display = val === null ? '<span class="db-null">NULL</span>' : escapeHtml(String(val));
                html += `<td class="db-td">${display}</td>`;
            }
            if (result.editable) {
                html += `<td class="db-td db-td-actions">
                    <button class="btn-sm btn-edit-row" data-id="${row.id}">✏️</button>
                    <button class="btn-sm btn-delete-row" data-id="${row.id}">🗑️</button>
                </td>`;
            }
            html += '</tr>';
        }
        html += '</tbody></table></div>';
        container.innerHTML = html;

        // Bind sort handlers (click on label area)
        container.querySelectorAll('.db-th-label').forEach(label => {
            label.style.cursor = 'pointer';
            label.addEventListener('click', (e) => {
                e.stopPropagation();
                const col = label.closest('.db-th').dataset.col;
                if (currentSortBy === col) {
                    currentSortOrder = currentSortOrder === 'asc' ? 'desc' : 'asc';
                } else {
                    currentSortBy = col;
                    currentSortOrder = 'asc';
                }
                loadTableData();
            });
        });

        // Bind filter button handlers
        container.querySelectorAll('.db-th-filter-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const colName = btn.dataset.col;
                const th = btn.closest('.db-th');
                openFilterDropdown(th, colName);
            });
        });

        // Bind edit/delete handlers
        container.querySelectorAll('.btn-edit-row').forEach(btn => {
            btn.addEventListener('click', () => openEditModal(parseInt(btn.dataset.id)));
        });
        container.querySelectorAll('.btn-delete-row').forEach(btn => {
            btn.addEventListener('click', () => confirmDelete(parseInt(btn.dataset.id)));
        });
    }

    function renderPagination(result) {
        const container = document.getElementById('db-pagination');
        if (result.total_pages <= 1) {
            container.style.display = 'none';
            return;
        }
        container.style.display = 'flex';

        let html = '';
        html += `<button class="btn-sm" ${currentPage <= 1 ? 'disabled' : ''} id="pg-prev">◀ 上一页</button>`;
        html += `<span class="pg-info">第 ${result.page} / ${result.total_pages} 页</span>`;
        html += `<button class="btn-sm" ${currentPage >= result.total_pages ? 'disabled' : ''} id="pg-next">下一页 ▶</button>`;
        container.innerHTML = html;

        document.getElementById('pg-prev')?.addEventListener('click', () => {
            if (currentPage > 1) { currentPage--; loadTableData(); }
        });
        document.getElementById('pg-next')?.addEventListener('click', () => {
            if (currentPage < result.total_pages) { currentPage++; loadTableData(); }
        });
    }

    async function openEditModal(rowId) {
        try {
            const result = await apiGet(`/api/db/tables/${activeTable}/${rowId}`);
            const row = result.data;
            const cols = result.columns;

            let formHtml = '<div class="db-edit-form">';
            for (const col of cols) {
                const val = row[col.name] ?? '';
                const readonly = col.pk ? 'readonly' : '';
                formHtml += `
                    <div class="db-edit-field">
                        <label>${escapeHtml(col.name)} <span class="db-field-type">${col.type}</span></label>
                        <textarea id="edit-${col.name}" ${readonly} rows="${String(val).length > 100 ? 4 : 1}">${escapeHtml(String(val))}</textarea>
                    </div>`;
            }
            formHtml += '</div>';

            showModal(
                `编辑 ${activeTable} #${rowId}`,
                formHtml,
                [
                    { label: '取消', class: 'btn btn-ghost', action: hideModal },
                    { label: '保存', class: 'btn btn-primary', action: () => saveRow(rowId, cols) },
                ]
            );
        } catch (e) {
            toast('加载行数据失败: ' + e.message, 'error');
        }
    }

    async function saveRow(rowId, cols) {
        const data = {};
        for (const col of cols) {
            if (col.pk) continue;
            const el = document.getElementById(`edit-${col.name}`);
            if (el) data[col.name] = el.value;
        }
        try {
            await apiPut(`/api/db/tables/${activeTable}/${rowId}`, { data });
            toast('保存成功', 'success');
            hideModal();
            // Invalidate cache for this table
            for (const key of Object.keys(columnValuesCache)) {
                if (key.startsWith(activeTable + '.')) delete columnValuesCache[key];
            }
            loadTableData();
        } catch (e) {
            toast('保存失败: ' + e.message, 'error');
        }
    }

    async function confirmDelete(rowId) {
        showModal(
            '确认删除',
            `<p>确定要删除 ${activeTable} 中 ID 为 ${rowId} 的记录吗？此操作不可撤销。</p>`,
            [
                { label: '取消', class: 'btn btn-ghost', action: hideModal },
                { label: '删除', class: 'btn btn-danger', action: async () => {
                    try {
                        await apiDelete(`/api/db/tables/${activeTable}/${rowId}`);
                        toast('删除成功', 'success');
                        hideModal();
                        // Invalidate cache for this table
                        for (const key of Object.keys(columnValuesCache)) {
                            if (key.startsWith(activeTable + '.')) delete columnValuesCache[key];
                        }
                        loadTableData();
                    } catch (e) {
                        toast('删除失败: ' + e.message, 'error');
                    }
                }},
            ]
        );
    }

    // ---- Modal helpers (reuse from PluginsModule pattern) ----
    function showModal(title, bodyHtml, buttons) {
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-body').innerHTML = bodyHtml;
        const footer = document.getElementById('modal-footer');
        footer.innerHTML = '';
        for (const b of buttons) {
            const btn = document.createElement('button');
            btn.className = b.class;
            btn.textContent = b.label;
            btn.addEventListener('click', b.action);
            footer.appendChild(btn);
        }
        document.getElementById('modal-overlay').classList.remove('hidden');
        document.getElementById('modal-close').onclick = hideModal;
        document.getElementById('modal-overlay').addEventListener('click', (e) => {
            if (e.target === document.getElementById('modal-overlay')) hideModal();
        });
    }

    function hideModal() {
        document.getElementById('modal-overlay').classList.add('hidden');
    }

    return { init, refresh };
})();
