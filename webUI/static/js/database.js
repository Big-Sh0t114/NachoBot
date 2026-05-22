/**
 * NachoBot WebUI — Database Module
 * Browse and manage SQLite database tables.
 */

const DatabaseModule = (() => {
    let tables = [];
    let activeTable = null;
    let currentPage = 1;
    let currentSearch = '';
    let currentSortBy = 'id';
    let currentSortOrder = 'desc';
    let searchTimeout = null;

    function init() {
        document.getElementById('db-search').addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                currentSearch = e.target.value;
                currentPage = 1;
                loadTableData();
            }, 400);
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
                currentSearch = '';
                document.getElementById('db-search').value = '';
                renderTableList();
                loadTableData();
            });
            container.appendChild(item);
        }
    }

    async function loadTableData() {
        if (!activeTable) return;

        const toolbar = document.getElementById('db-toolbar');
        toolbar.style.display = 'flex';

        try {
            const params = new URLSearchParams({
                page: currentPage,
                size: 50,
                search: currentSearch,
                sort_by: currentSortBy,
                sort_order: currentSortOrder,
            });
            const result = await apiGet(`/api/db/tables/${activeTable}?${params}`);
            renderTable(result);
            renderPagination(result);

            document.getElementById('db-table-info').textContent =
                `${result.label} — 共 ${result.total.toLocaleString()} 条${result.editable ? ' (可编辑)' : ' (只读)'}`;
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

        // Headers
        for (const col of cols) {
            const isSort = col.name === currentSortBy;
            const arrow = isSort ? (currentSortOrder === 'asc' ? ' ▲' : ' ▼') : '';
            html += `<th class="db-th ${isSort ? 'sorted' : ''}" data-col="${col.name}">${escapeHtml(col.name)}${arrow}</th>`;
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

        // Bind sort handlers
        container.querySelectorAll('.db-th[data-col]').forEach(th => {
            th.style.cursor = 'pointer';
            th.addEventListener('click', () => {
                const col = th.dataset.col;
                if (currentSortBy === col) {
                    currentSortOrder = currentSortOrder === 'asc' ? 'desc' : 'asc';
                } else {
                    currentSortBy = col;
                    currentSortOrder = 'asc';
                }
                loadTableData();
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
