import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { toast } from 'sonner';
import {
    ArrowLeft, Plus, PencilSimple, Trash, X, Square,
    DownloadSimple, UploadSimple, ArrowsClockwise, FunnelSimple,
} from '@phosphor-icons/react';
import ConfirmDeleteModal from '../components/ConfirmDeleteModal';
import ProgressDialog from '../components/ProgressDialog';

const API = process.env.REACT_APP_BACKEND_URL;
const TS = `${API}/api/team-score`;

// iter157 — Default fallback column layout used ONLY when no import has
// run yet. Once the admin imports the Consolidated workbook, the full
// 114-column Excel-order schema from `/api/team-score/schema` takes over
// and this array is no longer consulted.
const BASE_COLS = [
    { key: 'name', label: 'Name' },
    { key: 'email', label: 'Email ID' },
    { key: 'linkedin_id', label: 'LinkedIn ID' },
    { key: 'role', label: 'Role' },
    { key: 'joining_date', label: 'Joining Date' },
    { key: 'college', label: 'College' },
    { key: 'nirf_rank', label: 'NIRF Rank' },
    { key: 'degree', label: 'Degree' },
    { key: 'passing_year', label: 'Passing Year' },
];

// iter158 — Compact labelled field used in the redesigned filter row.
const FilterField = ({ label, testid, children }) => (
    <div className="flex flex-col gap-1 min-w-[140px] flex-1" data-testid={testid}>
        <label className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider">{label}</label>
        {children}
    </div>
);

export default function TeamScore() {
    const navigate = useNavigate();
    const [rounds, setRounds] = useState([]);
    const [employees, setEmployees] = useState([]);
    const [filterOpts, setFilterOpts] = useState({});
    // iter158 — `name` filter dropdown removed from the UI (duplicated the
    // top search bar); we keep the field in state so legacy `/employees`
    // query composition stays untouched but it is no longer user-settable.
    const [filters, setFilters] = useState({ employee_status: '', team: '', name: '', email: '', role: '', nirf_rank: '' });
    // iter156 — totals for filtered vs total badge and unnamed admin review.
    const [counts, setCounts] = useState({ filtered: 0, total: 0 });
    const [unnamedRows, setUnnamedRows] = useState([]);
    const [showUnnamed, setShowUnnamed] = useState(false);
    // iter157 — authoritative Excel-order schema fetched from the backend.
    const [columnSchema, setColumnSchema] = useState([]);
    // iter158 — multi-select employee state: `selected` is the array of
    // confirmed-chip employees that drive the `employee_ids` filter;
    // `search` is the uncommitted keyword in the input; never treat the
    // typed keyword as a selected employee.
    const [selected, setSelected] = useState([]);
    const [search, setSearch] = useState('');
    const [suggestions, setSuggestions] = useState([]);
    const [suggestOpen, setSuggestOpen] = useState(false);
    const [highlightIdx, setHighlightIdx] = useState(-1);
    const suggestTimerRef = useRef();
    const searchBoxRef = useRef();
    const [showRoundsModal, setShowRoundsModal] = useState(false);
    const [showEmpModal, setShowEmpModal] = useState(false);
    const [editingEmp, setEditingEmp] = useState(null);   // iter138 — edit-mode employee
    const [deleteModal, setDeleteModal] = useState(null); // iter138 — { emp } for delete confirm
    const [statusModal, setStatusModal] = useState(null);  // { emp }
    // iter155 — Shared progress dialog for import & export.
    const [progress, setProgress] = useState(null);
    const [loading, setLoading] = useState(false);
    const importRef = useRef();

    // iter136 — pagination.
    const PAGE_SIZE_OPTIONS = [10, 25, 50, 100, 150, 200, 250, 500];
    const [pageSize, setPageSize] = useState(50);
    const [page, setPage] = useState(1);
    const [pageInput, setPageInput] = useState('');

    const sortedRounds = useMemo(() => [...rounds].sort((a, b) => a.round_name.localeCompare(b.round_name)), [rounds]);

    // iter136 — pagination math.
    const totalRecords = employees.length;
    const totalPages = Math.max(1, Math.ceil(totalRecords / pageSize));
    const currentPage = Math.min(Math.max(1, page), totalPages);
    const pagedEmployees = useMemo(() => {
        const start = (currentPage - 1) * pageSize;
        return employees.slice(start, start + pageSize);
    }, [employees, currentPage, pageSize]);

    const goToPage = (p) => {
        const target = Math.min(Math.max(1, Math.floor(Number(p) || 1)), totalPages);
        setPage(target);
    };

    const fetchAll = useCallback(async () => {
        // Read the latest filters via the ref so this callback can stay
        // dependency-free (and thus reference-stable). That lets the
        // mount-only useEffect below list `fetchAll` as a dependency
        // without triggering a refetch every time filters change — the
        // explicit Filter / Reset buttons drive subsequent reloads.
        const currentFilters = filtersRef.current;
        setLoading(true);
        try {
            const q = new URLSearchParams();
            Object.entries(currentFilters).forEach(([k, v]) => { if (v) q.append(k, v); });
            const [rR, rE, rF, rU, rS] = await Promise.all([
                axios.get(`${TS}/rounds`, { withCredentials: true }),
                axios.get(`${TS}/employees?${q.toString()}`, { withCredentials: true }),
                axios.get(`${TS}/filters`, { withCredentials: true }),
                axios.get(`${TS}/unnamed`, { withCredentials: true }).catch(() => ({ data: { unnamed: [] } })),
                axios.get(`${TS}/schema`, { withCredentials: true }).catch(() => ({ data: { column_schema: [] } })),
            ]);
            setRounds(rR.data.rounds || []);
            setEmployees(rE.data.employees || []);
            setCounts({ filtered: rE.data.filtered_count ?? (rE.data.employees || []).length, total: rE.data.total_count ?? 0 });
            setFilterOpts(rF.data || {});
            setUnnamedRows(rU.data?.unnamed || []);
            setColumnSchema(rS.data?.column_schema || []);
            setPage(1);  // iter136 — reset to first page on any reload
        } catch (e) { toast.error(e.response?.data?.detail || 'Failed to load'); }
        setLoading(false);
    }, []);

    // Keep a live ref so `fetchAll` (stable) can read the latest filters.
    const filtersRef = useRef(filters);
    useEffect(() => { filtersRef.current = filters; }, [filters]);

    // Mount-only initial load. `fetchAll` is reference-stable, so this
    // satisfies react-hooks/exhaustive-deps without re-running.
    useEffect(() => { fetchAll(); }, [fetchAll]);

    const handleFilter = () => fetchAll();
    const handleReset = () => {
        setFilters({ employee_status: '', team: '', name: '', email: '', role: '', nirf_rank: '' });
        setSearch('');
        setSuggestions([]);
        setSuggestOpen(false);
        setSelected([]);
        setHighlightIdx(-1);
        setTimeout(fetchAll, 0);
    };

    // iter156/158 — Autocomplete query (debounced 200 ms).
    const onSearchChange = (v) => {
        setSearch(v);
        setSuggestOpen(true);
        setHighlightIdx(-1);
        if (suggestTimerRef.current) clearTimeout(suggestTimerRef.current);
        if (!v.trim()) { setSuggestions([]); return; }
        suggestTimerRef.current = setTimeout(async () => {
            try {
                const r = await axios.get(`${TS}/suggest`, {
                    params: { q: v, limit: 20 }, withCredentials: true,
                });
                setSuggestions(r.data.suggestions || []);
            } catch (_err) { setSuggestions([]); }
        }, 200);
    };

    // iter158 — Multi-select: toggle an employee into/out of the selected
    // chip set. Keeps the search keyword intact so the user can continue
    // searching for additional employees.
    const toggleEmployee = (s) => {
        setSelected(prev => {
            if (prev.find(p => p.id === s.id)) return prev.filter(p => p.id !== s.id);
            return [...prev, s];
        });
    };
    const removeSelected = (id) => setSelected(prev => prev.filter(p => p.id !== id));
    const clearAllSelected = () => setSelected([]);

    const onSearchKeyDown = (e) => {
        if (!suggestOpen || suggestions.length === 0) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); setHighlightIdx(i => Math.min(i + 1, suggestions.length - 1)); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); setHighlightIdx(i => Math.max(i - 1, 0)); }
        else if (e.key === 'Enter' && highlightIdx >= 0) { e.preventDefault(); toggleEmployee(suggestions[highlightIdx]); }
        else if (e.key === 'Escape') { setSuggestOpen(false); }
    };

    const selectedIds = useMemo(() => new Set(selected.map(s => s.id)), [selected]);

    const handleExport = async (fmt) => {
        if (progress) return;
        setProgress({ title: 'Preparing export…', message: `Preparing ${fmt.toUpperCase()} download.`, variant: 'busy' });
        try {
            const q = new URLSearchParams({ fmt });
            Object.entries(filters).forEach(([k, v]) => { if (v) q.append(k, v); });
            const res = await axios.get(`${TS}/export?${q.toString()}`, {
                withCredentials: true, responseType: 'blob',
                onDownloadProgress: (evt) => {
                    if (!evt.total) return;
                    const pct = Math.round((evt.loaded * 100) / evt.total);
                    setProgress(p => p ? { ...p, percent: pct, message: 'Downloading…' } : p);
                },
            });
            const url = URL.createObjectURL(res.data);
            const a = document.createElement('a');
            a.href = url; a.download = `team_scores.${fmt}`; a.click();
            URL.revokeObjectURL(url);
            setProgress(null);
        } catch (err) {
            toast.error(err.response?.data?.detail || 'Export failed');
            setProgress(null);
        }
    };

    const handleImport = async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        if (progress) { e.target.value = ''; return; }
        setProgress({ title: 'Importing records…', message: `Uploading ${file.name}…`, variant: 'busy' });
        const fd = new FormData();
        fd.append('file', file);
        try {
            const r = await axios.post(`${TS}/import`, fd, {
                withCredentials: true,
                onUploadProgress: (evt) => {
                    if (!evt.total) return;
                    const pct = Math.round((evt.loaded * 100) / evt.total);
                    setProgress(p => p ? { ...p, percent: pct } : p);
                },
            });
            const clamped = r.data.scores_clamped || 0;
            const unnamed = r.data.unnamed_rows_captured || 0;
            toast.success(
                `Imported: +${r.data.inserted} / updated ${r.data.updated}`
                + (unnamed ? `; ${unnamed} unnamed row${unnamed === 1 ? '' : 's'} sent to admin review` : '')
                + (r.data.rounds_created?.length ? `; new rounds: ${r.data.rounds_created.length}` : '')
                + (clamped ? `; ${clamped} score${clamped === 1 ? '' : 's'} capped to round total` : '')
            );
            fetchAll();
            setProgress(null);
        } catch (err) {
            toast.error(err.response?.data?.detail || 'Import failed');
            setProgress(null);
        }
        e.target.value = '';
    };

    const toggleStatus = async (emp) => {
        const action = (emp.employee_status || 'active') === 'active' ? 'deactivate' : 'activate';
        try {
            await axios.post(`${TS}/employees/${emp.id}/${action}`, {}, { withCredentials: true });
            toast.success(action === 'activate' ? 'Reactivated' : 'Deactivated');
            setStatusModal(null);
            fetchAll();
        } catch (err) { toast.error(err.response?.data?.detail || 'Failed'); }
    };

    // iter138 — delete a single employee record.
    const deleteEmployee = async (emp) => {
        try {
            await axios.delete(`${TS}/employees/${emp.id}`, { withCredentials: true });
            toast.success('Employee deleted');
            setDeleteModal(null);
            fetchAll();
        } catch (err) { toast.error(err.response?.data?.detail || 'Failed'); }
    };

    const closeEmpModal = () => { setShowEmpModal(false); setEditingEmp(null); };

    // iter136b — Cell format mirrors the Export file: `score/total (pct%)`.
    // No total → just the raw score. Empty → "-".
    const pct = (score, total) => {
        if (score === undefined || score === null || score === '') return '-';
        if (!total || Number(total) <= 0) return `${score}`;
        const s = Number(score), t = Number(total);
        return `${score}/${total} (${((s / t) * 100).toFixed(2)}%)`;
    };

    // iter135 — Joining-date display helper. Stored as "yyyy-mm-dd";
    // shown to user as "dd-mm-yyyy". Empty / unknown → "-".
    const fmtJoiningDate = (s) => {
        if (!s) return '-';
        const v = String(s).split('T')[0].split(' ')[0];
        const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(v);
        if (m) return `${m[3].padStart(2, '0')}-${m[2].padStart(2, '0')}-${m[1]}`;
        const m2 = /^(\d{1,2})-(\d{1,2})-(\d{4})$/.exec(v);
        if (m2) return `${m2[1].padStart(2, '0')}-${m2[2].padStart(2, '0')}-${m2[3]}`;
        return v;
    };

    return (
        <div className="min-h-screen bg-zinc-950 text-white">
            <div className="border-b border-zinc-800 p-6 flex items-center gap-3">
                <button onClick={() => navigate(-1)} data-testid="ts-back" className="p-2 hover:bg-zinc-800"><ArrowLeft size={18} /></button>
                <h1 className="text-2xl font-light">Team Score</h1>
            </div>

            <div className="p-6 space-y-4">
                {/* iter158 — Unified Search + Filters card. One card, clean
                    spacing, no duplicated name dropdown. Employee multi-select
                    with chips at the top; other filters as a compact row below. */}
                <div ref={searchBoxRef} className="bg-zinc-900 border border-zinc-800 p-4 space-y-3" data-testid="ts-search-filter-card">
                    {/* Row 1: search label + chips + input */}
                    <div className="space-y-2">
                        <div className="flex items-center justify-between">
                            <label className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">Search Employees by Name or Email</label>
                            <div className="flex items-center gap-3 text-xs text-zinc-500">
                                <span data-testid="ts-count-badge">
                                    <span className="text-white font-medium">{counts.filtered}</span>
                                    <span className="text-zinc-500"> / </span>
                                    <span className="text-white font-medium">{counts.total}</span>
                                    <span className="text-zinc-500"> employees</span>
                                </span>
                                {unnamedRows.length > 0 && <span className="text-amber-400">{unnamedRows.length} unnamed pending review</span>}
                            </div>
                        </div>
                        <div className="relative">
                            <div className="flex flex-wrap items-center gap-1.5 bg-zinc-900 border border-zinc-700 focus-within:border-cyan-600 px-2 py-1.5 min-h-[42px]" data-testid="ts-search-wrap">
                                {selected.map(s => (
                                    <span key={s.id} data-testid={`ts-chip-${s.id}`}
                                        className="inline-flex items-center gap-1.5 bg-cyan-900/40 border border-cyan-700/60 text-cyan-100 text-xs pl-2 pr-1 py-0.5">
                                        <span>{s.name}</span>
                                        <span className={`text-[10px] px-1 ${s.employee_status === 'active' ? 'text-emerald-300' : 'text-red-300'}`}>
                                            {s.employee_status === 'active' ? '●' : '○'}
                                        </span>
                                        <button onClick={() => removeSelected(s.id)} title={`Remove ${s.name}`}
                                            data-testid={`ts-chip-remove-${s.id}`}
                                            className="p-0.5 hover:bg-cyan-800/60 text-cyan-200">
                                            <X size={12} />
                                        </button>
                                    </span>
                                ))}
                                <input
                                    value={search}
                                    onChange={e => onSearchChange(e.target.value)}
                                    onFocus={() => { if (suggestions.length) setSuggestOpen(true); }}
                                    onBlur={() => setTimeout(() => setSuggestOpen(false), 180)}
                                    onKeyDown={onSearchKeyDown}
                                    placeholder={selected.length ? 'Add another employee…' : 'Start typing — e.g. Kath …'}
                                    data-testid="ts-search-input"
                                    className="flex-1 min-w-[160px] bg-transparent px-1 py-1 text-sm focus:outline-none"
                                    autoComplete="off"
                                />
                                {selected.length > 0 && (
                                    <button onClick={clearAllSelected} data-testid="ts-chip-clear-all"
                                        className="ml-auto text-[11px] text-zinc-400 hover:text-white underline-offset-2 hover:underline px-1">
                                        Clear all
                                    </button>
                                )}
                            </div>
                            {suggestOpen && search.trim() !== '' && (
                                <ul data-testid="ts-search-suggestions"
                                    className="absolute z-40 top-full left-0 right-0 mt-1 max-h-72 overflow-y-auto bg-zinc-900 border border-zinc-700 shadow-xl">
                                    {suggestions.length === 0 && (
                                        <li className="px-3 py-3 text-sm text-zinc-500" data-testid="ts-search-empty">
                                            No employees found for "{search}"
                                        </li>
                                    )}
                                    {suggestions.map((s, idx) => {
                                        const isSel = selectedIds.has(s.id);
                                        const isHi = idx === highlightIdx;
                                        return (
                                            <li key={s.id}
                                                onMouseDown={e => { e.preventDefault(); toggleEmployee(s); }}
                                                onMouseEnter={() => setHighlightIdx(idx)}
                                                data-testid={`ts-search-suggest-${s.id}`}
                                                aria-selected={isSel}
                                                className={`px-3 py-2 cursor-pointer border-b border-zinc-800 last:border-b-0 ${isHi ? 'bg-zinc-800' : isSel ? 'bg-cyan-950/40' : 'hover:bg-zinc-800'}`}>
                                                <div className="flex items-center gap-3">
                                                    <input type="checkbox" readOnly checked={isSel}
                                                        className="pointer-events-none accent-cyan-600" />
                                                    <div className="min-w-0 flex-1">
                                                        <div className="text-sm text-white truncate">{s.name || '—'}</div>
                                                        <div className="text-xs text-zinc-500 truncate">
                                                            {s.email || '(no email)'} · {s.team || '—'} · <span className={s.employee_status === 'active' ? 'text-emerald-400' : 'text-red-400'}>{s.employee_status}</span>
                                                        </div>
                                                    </div>
                                                    {s.role ? <span className="text-xs text-zinc-500 whitespace-nowrap">{s.role}</span> : null}
                                                </div>
                                            </li>
                                        );
                                    })}
                                </ul>
                            )}
                        </div>
                    </div>

                    {/* Row 2: Filter dropdowns + Filter/Reset buttons. Name
                        dropdown intentionally omitted — top search bar is the
                        single interface for employee selection. */}
                    <div className="flex flex-wrap items-end gap-2 pt-1 border-t border-zinc-800/60" data-testid="ts-filters">
                        <FilterField label="Team" testid="ts-filter-team">
                            <select value={filters.team} onChange={e => setFilters({ ...filters, team: e.target.value })}
                                data-testid="ts-filter-team" className="bg-zinc-900 border border-zinc-700 h-9 px-2 text-sm w-full focus:outline-none focus:border-cyan-600">
                                <option value="">All</option>
                                {(filterOpts.team || []).map(v => <option key={v} value={v}>{v}</option>)}
                            </select>
                        </FilterField>
                        <FilterField label="Status" testid="ts-filter-status-wrap">
                            <select value={filters.employee_status} onChange={e => setFilters({ ...filters, employee_status: e.target.value })}
                                data-testid="ts-filter-status" className="bg-zinc-900 border border-zinc-700 h-9 px-2 text-sm w-full focus:outline-none focus:border-cyan-600">
                                <option value="">All</option>
                                {(filterOpts.employee_status || []).map(v => <option key={v} value={v}>{v}</option>)}
                            </select>
                        </FilterField>
                        <FilterField label="Email" testid="ts-filter-email-wrap">
                            <select value={filters.email} onChange={e => setFilters({ ...filters, email: e.target.value })}
                                data-testid="ts-filter-email" className="bg-zinc-900 border border-zinc-700 h-9 px-2 text-sm w-full focus:outline-none focus:border-cyan-600">
                                <option value="">All</option>
                                {(filterOpts.email || []).map(v => <option key={v} value={v}>{v}</option>)}
                            </select>
                        </FilterField>
                        <FilterField label="Role" testid="ts-filter-role-wrap">
                            <select value={filters.role} onChange={e => setFilters({ ...filters, role: e.target.value })}
                                data-testid="ts-filter-role" className="bg-zinc-900 border border-zinc-700 h-9 px-2 text-sm w-full focus:outline-none focus:border-cyan-600">
                                <option value="">All</option>
                                {(filterOpts.role || []).map(v => <option key={v} value={v}>{v}</option>)}
                            </select>
                        </FilterField>
                        <FilterField label="NIRF Rank" testid="ts-filter-nirf-wrap">
                            <select value={filters.nirf_rank} onChange={e => setFilters({ ...filters, nirf_rank: e.target.value })}
                                data-testid="ts-filter-nirf" className="bg-zinc-900 border border-zinc-700 h-9 px-2 text-sm w-full focus:outline-none focus:border-cyan-600">
                                <option value="">All</option>
                                {(filterOpts.nirf_rank || []).map(v => <option key={v} value={v}>{v}</option>)}
                            </select>
                        </FilterField>
                        <div className="flex items-center gap-2 ml-auto">
                            <button onClick={handleFilter} data-testid="ts-filter-apply"
                                className="h-9 px-4 bg-cyan-700 hover:bg-cyan-600 text-white text-sm font-medium inline-flex items-center gap-1.5 transition-colors">
                                <FunnelSimple size={14} />Apply
                            </button>
                            <button onClick={handleReset} data-testid="ts-filter-reset"
                                className="h-9 px-4 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-sm inline-flex items-center gap-1.5 transition-colors">
                                <ArrowsClockwise size={14} />Reset
                            </button>
                        </div>
                    </div>
                </div>

                {/* Action bar */}
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex gap-2 flex-wrap">
                        <input ref={importRef} type="file" accept=".csv,.xlsx,.xls" onChange={handleImport} className="hidden" data-testid="ts-import-input" />
                        <button onClick={() => importRef.current?.click()} data-testid="ts-import-btn" className="px-3 py-2 bg-zinc-800 border border-zinc-700 hover:bg-zinc-700 text-sm flex items-center gap-1"><UploadSimple size={14} />Import</button>
                        <button onClick={() => handleExport('csv')} data-testid="ts-export-csv" className="px-3 py-2 bg-zinc-800 border border-zinc-700 hover:bg-zinc-700 text-sm flex items-center gap-1"><DownloadSimple size={14} />Export CSV</button>
                        <button onClick={() => handleExport('xlsx')} data-testid="ts-export-xlsx" className="px-3 py-2 bg-zinc-800 border border-zinc-700 hover:bg-zinc-700 text-sm flex items-center gap-1"><DownloadSimple size={14} />Export XLSX</button>
                    </div>
                    <div className="flex gap-2 flex-wrap">
                        <button onClick={() => setShowEmpModal(true)} data-testid="ts-add-emp" className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-sm flex items-center gap-1"><Plus size={14} />Add New Employee Team Score</button>
                        <button onClick={() => setShowRoundsModal(true)} data-testid="ts-add-round" className="px-4 py-2 bg-cyan-700 hover:bg-cyan-600 text-sm flex items-center gap-1"><Plus size={14} />Add New Team Round</button>
                    </div>
                </div>

                {/* Table — iter136: sticky header (vertical) + sticky first 3
                    columns (Status, Name, Email ID) horizontally. iter157:
                    when the authoritative Excel-order schema is present we
                    render all 117-style columns from it; otherwise fall back
                    to the legacy BASE_COLS + round-sort layout. */}
                <div className="bg-zinc-900 border border-zinc-800 overflow-auto max-h-[calc(100vh-360px)]" data-testid="ts-table-wrap">
                    <table className="text-sm border-separate border-spacing-0">
                        {columnSchema.length > 0 ? (
                            <>
                                <thead>
                                    <tr className="text-xs uppercase tracking-wider text-zinc-500">
                                        <th data-testid="ts-th-status" className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 left-0 z-30 w-[64px] min-w-[64px]">Status</th>
                                        {columnSchema.map((col) => {
                                            const nameIdx = columnSchema.findIndex(c => (c.header || '').toLowerCase() === 'name');
                                            const isNameCol = col.col === nameIdx;
                                            const sticky = isNameCol
                                                ? 'sticky top-0 left-[64px] z-30 w-[200px] min-w-[200px] shadow-[inset_-1px_0_0_rgba(63,63,70,1)]'
                                                : 'sticky top-0 z-20';
                                            return (
                                                <th
                                                    key={col.column_id}
                                                    data-testid={`ts-th-${col.column_id}`}
                                                    data-col-letter={col.letter}
                                                    title={`Excel column ${col.letter} (position ${col.col + 1}) — ${col.header || '(blank header)'}`}
                                                    className={`px-3 py-3 text-left whitespace-pre-line align-bottom bg-zinc-900 border-b border-zinc-800 ${sticky}`}>
                                                    <div className="text-[10px] text-zinc-600 font-normal normal-case tracking-normal">{col.letter}</div>
                                                    <div>{col.header || <span className="text-zinc-600">(blank)</span>}</div>
                                                </th>
                                            );
                                        })}
                                        <th data-testid="ts-th-actions" className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 z-20">Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {loading && <tr><td colSpan={columnSchema.length + 2} className="text-center py-6 text-zinc-500 bg-zinc-900">Loading...</td></tr>}
                                    {!loading && employees.length === 0 && <tr><td colSpan={columnSchema.length + 2} className="text-center py-8 text-zinc-500 bg-zinc-900">No employees match the current filters.</td></tr>}
                                    {!loading && pagedEmployees.map(e => {
                                        const active = (e.employee_status || 'active') === 'active';
                                        const rawByCol = {};
                                        (e.raw_columns || []).forEach(rc => { rawByCol[rc.col] = rc.value; });
                                        const nameIdx = columnSchema.findIndex(c => (c.header || '').toLowerCase() === 'name');
                                        return (
                                            <tr key={e.id} className="group" data-testid={`ts-row-${e.id}`}>
                                                <td className="px-3 py-3 bg-zinc-900 group-hover:bg-zinc-800 border-b border-zinc-800/50 sticky left-0 z-10 w-[64px] min-w-[64px] transition-colors">
                                                    <button onClick={() => setStatusModal({ emp: e })}
                                                        data-testid={`ts-status-${e.id}`}
                                                        title={active ? 'Active' : 'Inactive'}
                                                        className={`p-1 transition-colors ${active ? 'text-emerald-400 hover:bg-emerald-900/30' : 'text-red-400 hover:bg-red-900/30'}`}>
                                                        <Square size={16} weight="fill" />
                                                    </button>
                                                </td>
                                                {columnSchema.map((col) => {
                                                    const v = rawByCol[col.col];
                                                    const display = (v === '' || v === null || v === undefined) ? '-' : String(v);
                                                    const isNameCol = col.col === nameIdx;
                                                    const sticky = isNameCol
                                                        ? 'sticky left-[64px] z-10 w-[200px] min-w-[200px] shadow-[inset_-1px_0_0_rgba(63,63,70,1)]'
                                                        : '';
                                                    return (
                                                        <td
                                                            key={col.column_id}
                                                            data-testid={`ts-${e.id}-${col.column_id}`}
                                                            className={`px-3 py-3 whitespace-nowrap bg-zinc-900 group-hover:bg-zinc-800 border-b border-zinc-800/50 transition-colors ${sticky}`}>
                                                            {display}
                                                        </td>
                                                    );
                                                })}
                                                <td className="px-3 py-3 whitespace-nowrap border-b border-zinc-800/50 group-hover:bg-zinc-800/30">
                                                    <div className="flex items-center gap-2">
                                                        <button onClick={() => { setEditingEmp(e); setShowEmpModal(true); }}
                                                            data-testid={`ts-edit-${e.id}`}
                                                            title="Edit employee"
                                                            className="p-1.5 text-cyan-400 hover:bg-cyan-900/30 transition-colors">
                                                            <PencilSimple size={16} />
                                                        </button>
                                                        <button onClick={() => setDeleteModal({ emp: e })}
                                                            data-testid={`ts-delete-${e.id}`}
                                                            title="Delete employee"
                                                            className="p-1.5 text-red-400 hover:bg-red-900/30 transition-colors">
                                                            <Trash size={16} />
                                                        </button>
                                                    </div>
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </>
                        ) : (
                            <>
                        <thead>
                            <tr className="text-xs uppercase tracking-wider text-zinc-500">
                                <th data-testid="ts-th-status" className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 left-0 z-30 w-[64px] min-w-[64px]">Status</th>
                                <th data-testid="ts-th-name" className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 left-[64px] z-30 w-[180px] min-w-[180px] shadow-[inset_-1px_0_0_rgba(63,63,70,0.5)]">Name</th>
                                <th data-testid="ts-th-email" className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 left-[244px] z-30 w-[240px] min-w-[240px] shadow-[inset_-1px_0_0_rgba(63,63,70,1)]">Email ID</th>
                                {BASE_COLS.filter(c => c.key !== 'name' && c.key !== 'email').map(c =>
                                    <th key={c.key} className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 z-20">{c.label}</th>
                                )}
                                {sortedRounds.map(r => {
                                    const t = r.total_score;
                                    const suffix = (t === null || t === undefined || t === '' || Number(t) <= 0) ? '' : `(${t})`;
                                    return (
                                        <th key={r.id} className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 z-20">{r.round_name}{suffix}</th>
                                    );
                                })}
                                <th data-testid="ts-th-actions" className="px-3 py-3 text-left whitespace-nowrap bg-zinc-900 border-b border-zinc-800 sticky top-0 z-20">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {loading && <tr><td colSpan={BASE_COLS.length + sortedRounds.length + 2} className="text-center py-6 text-zinc-500 bg-zinc-900">Loading...</td></tr>}
                            {!loading && employees.length === 0 && <tr><td colSpan={BASE_COLS.length + sortedRounds.length + 2} className="text-center py-8 text-zinc-500 bg-zinc-900">No employees yet — add your first one →</td></tr>}
                            {!loading && pagedEmployees.map(e => {
                                const active = (e.employee_status || 'active') === 'active';
                                return (
                                    <tr key={e.id} className="group" data-testid={`ts-row-${e.id}`}>
                                        <td className="px-3 py-3 bg-zinc-900 group-hover:bg-zinc-800 border-b border-zinc-800/50 sticky left-0 z-10 w-[64px] min-w-[64px] transition-colors">
                                            <button onClick={() => setStatusModal({ emp: e })}
                                                data-testid={`ts-status-${e.id}`}
                                                title={active ? 'Active' : 'Inactive'}
                                                className={`p-1 transition-colors ${active ? 'text-emerald-400 hover:bg-emerald-900/30' : 'text-red-400 hover:bg-red-900/30'}`}>
                                                <Square size={16} weight="fill" />
                                            </button>
                                        </td>
                                        <td className="px-3 py-3 whitespace-nowrap bg-zinc-900 group-hover:bg-zinc-800 border-b border-zinc-800/50 sticky left-[64px] z-10 w-[180px] min-w-[180px] transition-colors shadow-[inset_-1px_0_0_rgba(63,63,70,0.5)]">{e.name || '-'}</td>
                                        <td className="px-3 py-3 whitespace-nowrap bg-zinc-900 group-hover:bg-zinc-800 border-b border-zinc-800/50 sticky left-[244px] z-10 w-[240px] min-w-[240px] transition-colors shadow-[inset_-1px_0_0_rgba(63,63,70,1)]">{e.email || '-'}</td>
                                        {BASE_COLS.filter(c => c.key !== 'name' && c.key !== 'email').map(c => (
                                            <td key={c.key} className="px-3 py-3 whitespace-nowrap border-b border-zinc-800/50 group-hover:bg-zinc-800/30">
                                                {c.key === 'joining_date' ? fmtJoiningDate(e[c.key]) : (e[c.key] || '-')}
                                            </td>
                                        ))}
                                        {sortedRounds.map(r => {
                                            const v = e.round_scores?.[r.round_name];
                                            return <td key={r.id} className="px-3 py-3 whitespace-nowrap border-b border-zinc-800/50 group-hover:bg-zinc-800/30" data-testid={`ts-${e.id}-${r.round_name}`}>{pct(v, r.total_score)}</td>;
                                        })}
                                        <td className="px-3 py-3 whitespace-nowrap border-b border-zinc-800/50 group-hover:bg-zinc-800/30">
                                            <div className="flex items-center gap-2">
                                                <button onClick={() => { setEditingEmp(e); setShowEmpModal(true); }}
                                                    data-testid={`ts-edit-${e.id}`}
                                                    title="Edit employee"
                                                    className="p-1.5 text-cyan-400 hover:bg-cyan-900/30 transition-colors">
                                                    <PencilSimple size={16} />
                                                </button>
                                                <button onClick={() => setDeleteModal({ emp: e })}
                                                    data-testid={`ts-delete-${e.id}`}
                                                    title="Delete employee"
                                                    className="p-1.5 text-red-400 hover:bg-red-900/30 transition-colors">
                                                    <Trash size={16} />
                                                </button>
                                            </div>
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                            </>
                        )}
                    </table>
                </div>

                {/* iter136 — Pagination footer */}
                {!loading && totalRecords > 0 && (
                    <div className="flex flex-wrap items-center justify-between gap-3 text-sm" data-testid="ts-pagination">
                        <div className="flex items-center gap-2 text-zinc-400">
                            <span>Rows per page:</span>
                            <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}
                                data-testid="ts-page-size" className="bg-zinc-800 border border-zinc-700 px-2 py-1 text-sm">
                                {PAGE_SIZE_OPTIONS.map(n => <option key={n} value={n}>{n}</option>)}
                            </select>
                            <span className="ml-3">
                                Showing {(currentPage - 1) * pageSize + 1}–
                                {Math.min(currentPage * pageSize, totalRecords)} of {totalRecords}
                            </span>
                        </div>
                        <div className="flex items-center gap-1">
                            {currentPage > 1 && (
                                <>
                                    <button onClick={() => setPage(1)} data-testid="ts-page-first"
                                        className="px-2 py-1 bg-zinc-800 border border-zinc-700 hover:bg-zinc-700">&laquo;</button>
                                    <button onClick={() => setPage(currentPage - 1)} data-testid="ts-page-prev"
                                        className="px-2 py-1 bg-zinc-800 border border-zinc-700 hover:bg-zinc-700">&lsaquo;</button>
                                </>
                            )}
                            <span className="px-2 py-1 text-zinc-400" data-testid="ts-page-indicator">
                                Page {currentPage} / {totalPages}
                            </span>
                            {currentPage < totalPages && (
                                <>
                                    <button onClick={() => setPage(currentPage + 1)} data-testid="ts-page-next"
                                        className="px-2 py-1 bg-zinc-800 border border-zinc-700 hover:bg-zinc-700">&rsaquo;</button>
                                    <button onClick={() => setPage(totalPages)} data-testid="ts-page-last"
                                        className="px-2 py-1 bg-zinc-800 border border-zinc-700 hover:bg-zinc-700">&raquo;</button>
                                </>
                            )}
                            <form onSubmit={e => { e.preventDefault(); goToPage(pageInput); setPageInput(''); }}
                                className="flex items-center gap-1 ml-3">
                                <input value={pageInput} onChange={e => setPageInput(e.target.value)}
                                    placeholder="#" type="number" min="1" max={totalPages}
                                    data-testid="ts-page-input"
                                    className="w-16 bg-zinc-800 border border-zinc-700 px-2 py-1 text-sm" />
                                <button type="submit" data-testid="ts-page-go"
                                    className="px-2 py-1 bg-cyan-700 hover:bg-cyan-600 text-sm">Go</button>
                            </form>
                        </div>
                    </div>
                )}
                {/* iter156 — Admin Import Review: rows the latest import
                    captured without an employee name. Collapsed by default.
                    Never appear in the main table or in search. */}
                {unnamedRows.length > 0 && (
                    <div className="bg-zinc-900 border border-amber-900/50 p-4" data-testid="ts-unnamed-review">
                        <button onClick={() => setShowUnnamed(v => !v)}
                            data-testid="ts-unnamed-toggle"
                            className="w-full flex items-center justify-between text-left text-sm text-amber-300 hover:text-amber-200">
                            <span>Admin Import Review — {unnamedRows.length} row{unnamedRows.length === 1 ? '' : 's'} without an employee name</span>
                            <span className="text-xs text-amber-400">{showUnnamed ? 'Hide' : 'Show'}</span>
                        </button>
                        {showUnnamed && (
                            <div className="mt-3 overflow-x-auto">
                                <table className="w-full text-xs">
                                    <thead className="text-amber-300/70">
                                        <tr>
                                            <th className="text-left px-2 py-1">Source Row #</th>
                                            <th className="text-left px-2 py-1">Team</th>
                                            <th className="text-left px-2 py-1">Status</th>
                                            <th className="text-left px-2 py-1">S.No</th>
                                            <th className="text-left px-2 py-1">Role</th>
                                            <th className="text-left px-2 py-1">Email</th>
                                            <th className="text-left px-2 py-1">Notes</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {unnamedRows.map(u => (
                                            <tr key={u.id} data-testid={`ts-unnamed-row-${u.source_row_index}`} className="border-t border-zinc-800">
                                                <td className="px-2 py-1">{u.source_row_index + 1}</td>
                                                <td className="px-2 py-1">{u.team || '—'}</td>
                                                <td className="px-2 py-1">{u.employee_status || '—'}</td>
                                                <td className="px-2 py-1">{u.s_no || '—'}</td>
                                                <td className="px-2 py-1">{u.role || '—'}</td>
                                                <td className="px-2 py-1">{u.email || '—'}</td>
                                                <td className="px-2 py-1 text-zinc-500">{u.notes || '—'}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </div>
                )}
            </div>
            {showEmpModal && <EmployeeModal rounds={sortedRounds} editing={editingEmp} onClose={closeEmpModal} onChanged={fetchAll} />}
            {statusModal && <StatusModal emp={statusModal.emp} onConfirm={() => toggleStatus(statusModal.emp)} onClose={() => setStatusModal(null)} />}
            {deleteModal && <DeleteEmployeeModal emp={deleteModal.emp} onConfirm={() => deleteEmployee(deleteModal.emp)} onClose={() => setDeleteModal(null)} />}
            {/* iter155 — Shared progress dialog for import/export. */}
            <ProgressDialog
                open={!!progress}
                title={progress?.title || 'Working…'}
                message={progress?.message || 'Please wait…'}
                percent={progress?.percent}
                variant={progress?.variant || 'busy'}
                testId="team-score-progress-dialog"
            />
        </div>
    );
}

// ─────────────────────── Rounds Modal ──────────────────────────────────

function RoundsModal({ rounds, onClose, onChanged }) {
    const [name, setName] = useState('');
    const [total, setTotal] = useState('');
    const [editing, setEditing] = useState(null);
    // iter146 — secure delete confirmation target (replaces window.confirm).
    const [deleteTarget, setDeleteTarget] = useState(null); // round | null

    const submit = async () => {
        if (!name.trim()) return toast.error('Round name required');
        try {
            if (editing) {
                await axios.put(`${TS}/rounds/${editing.id}`, { round_name: name, total_score: parseFloat(total) || 0 }, { withCredentials: true });
                toast.success('Updated');
            } else {
                await axios.post(`${TS}/rounds`, { round_name: name, total_score: parseFloat(total) || 0 }, { withCredentials: true });
                toast.success('Added');
            }
            setName(''); setTotal(''); setEditing(null);
            onChanged();
        } catch (err) { toast.error(err.response?.data?.detail || 'Failed'); }
    };

    const remove = async (r) => {
        try {
            await axios.delete(`${TS}/rounds/${r.id}`, { withCredentials: true });
            toast.success('Deleted');
            setDeleteTarget(null);
            onChanged();
        } catch (err) { toast.error(err.response?.data?.detail || 'Failed'); }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" data-testid="ts-rounds-modal">
            <div className="bg-zinc-900 border border-zinc-700 w-full max-w-lg flex flex-col max-h-[90vh] min-h-0">
                {/* Header — never scrolls. */}
                <div className="flex items-center justify-between p-6 pb-4 shrink-0 border-b border-zinc-800">
                    <h2 className="text-lg font-semibold">{editing ? 'Edit Team Round' : 'Add New Team Round'}</h2>
                    <button onClick={onClose} className="text-zinc-500 hover:text-white"><X size={20} /></button>
                </div>

                {/* Body — only this scrolls when content overflows. */}
                <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-5">
                    <div className="space-y-3">
                        <input value={name} onChange={e => setName(e.target.value)} placeholder="Round Name (e.g. A)" data-testid="ts-round-name" className="w-full bg-zinc-800 border border-zinc-700 px-3 py-2 text-sm" />
                        <input value={total} onChange={e => setTotal(e.target.value)} placeholder="Total Score" type="number" step="0.01" data-testid="ts-round-total" className="w-full bg-zinc-800 border border-zinc-700 px-3 py-2 text-sm" />
                        <div className="flex justify-end gap-2">
                            {editing && <button onClick={() => { setEditing(null); setName(''); setTotal(''); }} className="px-3 py-2 bg-zinc-700 hover:bg-zinc-600 text-sm">Cancel</button>}
                            <button onClick={submit} data-testid="ts-round-submit" className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-sm">{editing ? 'Update' : 'Add'}</button>
                        </div>
                    </div>

                    {rounds.length > 0 && (
                        <div className="border-t border-zinc-800 pt-4">
                            <p className="text-xs text-zinc-500 uppercase tracking-wider mb-2">Existing Rounds</p>
                            <div className="flex flex-wrap gap-2">
                                {rounds.map(r => (
                                    <div key={r.id} className="flex items-center gap-2 bg-zinc-800 border border-zinc-700 px-3 py-1.5 text-xs" data-testid={`ts-round-card-${r.id}`}>
                                        <span className="whitespace-nowrap">{r.round_name} ({r.total_score})</span>
                                        <button onClick={() => { setEditing(r); setName(r.round_name); setTotal(String(r.total_score)); }}
                                            data-testid={`ts-round-edit-${r.id}`} className="text-zinc-400 hover:text-white"><PencilSimple size={12} /></button>
                                        <button onClick={() => setDeleteTarget(r)} data-testid={`ts-round-del-${r.id}`} className="text-zinc-400 hover:text-red-400"><Trash size={12} /></button>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            </div>
            <ConfirmDeleteModal
                open={!!deleteTarget}
                title="Delete Team Round?"
                itemLabel={deleteTarget?.round_name}
                description="This will remove the round permanently. Any score data already recorded against this round will be detached."
                testId="ts-delete-round"
                onConfirm={() => remove(deleteTarget)}
                onClose={() => setDeleteTarget(null)}
            />
        </div>
    );
}

// ─────────────────────── Employee Modal ──────────────────────────────────

function EmployeeModal({ rounds, editing, onClose, onChanged }) {
    // iter138 — `editing` is the employee being edited (null on Add).
    const isEdit = !!editing;
    const [form, setForm] = useState(() => {
        if (editing) {
            return {
                name: editing.name || '', email: editing.email || '',
                linkedin_id: editing.linkedin_id || '', role: editing.role || '',
                joining_date: editing.joining_date || '',
                college: editing.college || '', nirf_rank: editing.nirf_rank || '',
                degree: editing.degree || '', passing_year: editing.passing_year || '',
            };
        }
        return {
            name: '', email: '', linkedin_id: '', role: '', joining_date: '',
            college: '', nirf_rank: '', degree: '', passing_year: '',
        };
    });
    const [pairs, setPairs] = useState(() => {
        if (editing && editing.round_scores && Object.keys(editing.round_scores).length) {
            return Object.entries(editing.round_scores).map(([rn, s]) => ({
                round_name: rn, score: String(s),
            }));
        }
        return [{ round_name: '', score: '' }];
    });

    const usedRounds = pairs.map(p => p.round_name).filter(Boolean);
    const availableRounds = (round_for_idx) => rounds.filter(r => !usedRounds.includes(r.round_name) || r.round_name === round_for_idx);

    const addPair = () => setPairs([...pairs, { round_name: '', score: '' }]);
    const updatePair = (i, field, v) => { const c = [...pairs]; c[i][field] = v; setPairs(c); };
    const removePair = (i) => setPairs(pairs.filter((_, idx) => idx !== i));

    const submit = async () => {
        if (!form.name.trim()) return toast.error('Name required');
        // iter137 — block submit on any out-of-range round score.
        for (const p of pairs) {
            if (!p.round_name || p.score === '') continue;
            const r = rounds.find(x => x.round_name === p.round_name);
            const t = r ? Number(r.total_score) : null;
            const s = Number(p.score);
            if (!Number.isFinite(s) || s < 0) {
                return toast.error(`Score for "${p.round_name}" cannot be below 0`);
            }
            if (t && t > 0 && s > t) {
                return toast.error(`Score for "${p.round_name}" cannot exceed ${t}`);
            }
        }
        const round_scores = {};
        for (const p of pairs) {
            if (p.round_name && p.score !== '') round_scores[p.round_name] = parseFloat(p.score);
        }
        try {
            if (isEdit) {
                await axios.put(`${TS}/employees/${editing.id}`, { ...form, round_scores }, { withCredentials: true });
                toast.success('Employee updated');
            } else {
                await axios.post(`${TS}/employees`, { ...form, round_scores }, { withCredentials: true });
                toast.success('Employee added');
            }
            onClose();
            onChanged();
        } catch (err) { toast.error(err.response?.data?.detail || 'Failed'); }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" data-testid="ts-emp-modal">
            <div className="bg-zinc-900 border border-zinc-700 w-full max-w-2xl flex flex-col max-h-[90vh] min-h-0">
                {/* Header — pinned. */}
                <div className="flex items-center justify-between p-6 pb-4 shrink-0 border-b border-zinc-800">
                    <h2 className="text-lg font-semibold">{isEdit ? 'Edit Employee Team Score' : 'Add New Employee Team Score'}</h2>
                    <button onClick={onClose} className="text-zinc-500 hover:text-white shrink-0"><X size={20} /></button>
                </div>

                {/* Scrollable body — `min-h-0` is REQUIRED for a flex child
                    to honour its parent's max-h and overflow correctly. */}
                <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-4" data-testid="ts-emp-modal-body">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {BASE_COLS.map(c => (
                            <div key={c.key}>
                                <label className="text-xs text-zinc-500 uppercase tracking-wider">{c.label}</label>
                                <input value={form[c.key] || ''} onChange={e => setForm({ ...form, [c.key]: e.target.value })}
                                    data-testid={`ts-emp-${c.key}`}
                                    type={c.key === 'joining_date' ? 'date' : 'text'}
                                    className="w-full mt-1 bg-zinc-800 border border-zinc-700 px-3 py-2 text-sm" />
                            </div>
                        ))}
                    </div>

                    <div className="border-t border-zinc-800 pt-4">
                        <div className="flex items-center justify-between mb-2">
                            <p className="text-xs text-zinc-500 uppercase tracking-wider">Round Scores</p>
                            <button onClick={addPair} data-testid="ts-emp-add-pair" className="text-xs px-2 py-1 bg-cyan-800 hover:bg-cyan-700 flex items-center gap-1"><Plus size={12} />Add Round</button>
                        </div>
                        <div className="space-y-2">
                            {pairs.map((p, i) => {
                                // iter137 — per-pair score validation.
                                const selectedRound = rounds.find(r => r.round_name === p.round_name);
                                const total = selectedRound ? Number(selectedRound.total_score) : null;
                                const scoreNum = p.score === '' ? null : Number(p.score);
                                let err = '';
                                if (scoreNum !== null && Number.isFinite(scoreNum)) {
                                    if (scoreNum < 0) err = 'Score cannot be below 0';
                                    else if (total && total > 0 && scoreNum > total) err = `Score cannot exceed total (${total})`;
                                }
                                return (
                                    <div key={i} className="flex flex-col gap-1">
                                        <div className="flex gap-2 items-center">
                                            <select value={p.round_name} onChange={e => updatePair(i, 'round_name', e.target.value)}
                                                data-testid={`ts-emp-pair-round-${i}`} className="flex-1 min-w-0 bg-zinc-800 border border-zinc-700 px-2 py-2 text-sm">
                                                <option value="">— select round —</option>
                                                {availableRounds(p.round_name).map(r => <option key={r.id} value={r.round_name}>{r.round_name} (Total: {r.total_score})</option>)}
                                            </select>
                                            <input value={p.score} onChange={e => updatePair(i, 'score', e.target.value)}
                                                placeholder="Score" type="number" step="0.01"
                                                min={0}
                                                max={total && total > 0 ? total : undefined}
                                                data-testid={`ts-emp-pair-score-${i}`}
                                                className={`w-28 shrink-0 bg-zinc-800 border px-2 py-2 text-sm ${err ? 'border-red-500' : 'border-zinc-700'}`} />
                                            {pairs.length > 1 && <button onClick={() => removePair(i)} className="text-red-400 hover:text-red-300 shrink-0"><X size={16} /></button>}
                                        </div>
                                        {err && (
                                            <p data-testid={`ts-emp-pair-error-${i}`} className="text-xs text-red-400 pl-1">{err}</p>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                </div>

                {/* Footer — pinned, always visible regardless of pair count. */}
                <div className="flex justify-end gap-2 p-6 pt-4 shrink-0 border-t border-zinc-800 bg-zinc-900">
                    <button onClick={onClose} className="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 text-sm">Cancel</button>
                    <button onClick={submit} data-testid="ts-emp-submit" className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-sm">{isEdit ? 'Update' : 'Add'}</button>
                </div>
            </div>
        </div>
    );
}

// ─────────────────────── Delete Employee Modal ──────────────────────────

function DeleteEmployeeModal({ emp, onConfirm, onClose }) {
    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" data-testid="ts-delete-modal">
            <div className="bg-zinc-900 border border-zinc-700 w-full max-w-sm mx-4 p-6 space-y-4">
                <div className="flex items-center justify-between">
                    <h2 className="text-lg font-semibold">Delete Employee?</h2>
                    <button onClick={onClose} className="text-zinc-500 hover:text-white"><X size={20} /></button>
                </div>
                <p className="text-sm text-zinc-300">
                    This will permanently remove
                    <span className="text-white font-medium mx-1">{emp.name}</span>
                    {emp.email ? <span className="text-zinc-400">({emp.email})</span> : null}
                    from the Team Score table. This action cannot be undone.
                </p>
                <div className="flex justify-end gap-2">
                    <button onClick={onClose} className="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 text-sm">Cancel</button>
                    <button onClick={onConfirm} data-testid="ts-delete-confirm"
                        className="px-4 py-2 bg-red-700 hover:bg-red-600 text-white text-sm">
                        Delete
                    </button>
                </div>
            </div>
        </div>
    );
}

// ─────────────────────── Status Modal ──────────────────────────────────

function StatusModal({ emp, onConfirm, onClose }) {
    const active = (emp.employee_status || 'active') === 'active';
    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" data-testid="ts-status-modal">
            <div className="bg-zinc-900 border border-zinc-700 w-full max-w-sm mx-4 p-6 space-y-4">
                <h2 className="text-lg font-semibold">{emp.name}</h2>
                <p className="text-sm">
                    <span className="text-zinc-500 mr-2">Current Status:</span>
                    <span className={active ? 'text-emerald-400' : 'text-red-400'}>{active ? 'Active' : 'Inactive'}</span>
                </p>
                <div className="flex justify-end gap-2">
                    <button onClick={onClose} className="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 text-sm">Cancel</button>
                    <button onClick={onConfirm} data-testid="ts-status-confirm"
                        className={`px-4 py-2 text-sm text-white ${active ? 'bg-red-700 hover:bg-red-600' : 'bg-emerald-700 hover:bg-emerald-600'}`}>
                        {active ? 'Deactivate' : 'Reactivate'}
                    </button>
                </div>
            </div>
        </div>
    );
}
