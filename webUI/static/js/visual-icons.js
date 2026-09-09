/**
 * NachoBot WebUI — Local SVG icon bridge
 *
 * Keeps application chrome visually consistent without changing user chat,
 * logs, editor text, or plugin documentation.  Legacy glyphs are converted
 * at render time so dynamically-created controls receive the same treatment.
 */
(function () {
    'use strict';

    const SPRITE_URL = '/static/nacho-ui-icons.svg';
    const GLYPH_TO_ICON = {
        '📝': 'file-text',
        '🚀': 'rocket',
        '🖥': 'terminal',
        '🧩': 'puzzle',
        '🗄': 'database',
        '📚': 'books',
        '🧠': 'brain',
        '🧙': 'wand-sparkles',
        '📄': 'file-text',
        '🗑': 'trash',
        '📥': 'download',
        '☘': 'sparkles',
        '⏳': 'loader',
        '🔍': 'search',
        '🔧': 'wrench',
        '💪': 'sparkles',
        '🛡': 'shield-check',
        '♻': 'rotate-ccw',
        '❄': 'snowflake',
        '🔌': 'plug',
        '💻': 'cpu',
        '🐧': 'message-circle',
        '📺': 'monitor-play',
        '💬': 'message-circle',
        '🎤': 'microphone',
        '🎙': 'audio-lines',
        '💡': 'lightbulb',
        '🤖': 'bot',
        '👁': 'eye',
        '🔎': 'scan-search',
        '📦': 'package',
        '🔒': 'lock',
        '💎': 'gem',
        '🔗': 'link',
        '✅': 'circle-check',
        '\u221A': 'circle-check',
        '\u2713': 'circle-check',
        '\u2714': 'circle-check',
        '\u{1F3AE}': 'gamepad-2',
        '\u{1F4CB}': 'clipboard',
        '\u{1F4CA}': 'chart',
        '⚠': 'alert-triangle',
        '🔄': 'loader',
        '🎉': 'sparkles',
        '❌': 'circle-x',
        '❓': 'circle-help',
        '🟢': 'circle-check',
        '⏸': 'pause-circle',
        '📁': 'folder',
        'ℹ': 'info',
        '⚙': 'settings',
        '📖': 'book-open',
        '💾': 'save',
        '↩': 'undo',
        '✏': 'pencil',
        '🧬': 'orbit',
        '🕸': 'network',
        '🔈': 'volume-low',
        '⌕': 'search',
        '＋': 'plus',
        '◀': 'panel-left-close',
        '☰': 'menu',
        '↑': 'send',
        '→': 'arrow-right',
        '←': 'arrow-left',
        '✕': 'x',
    };

    const ICON_NAMES = new Set([...Object.values(GLYPH_TO_ICON), 'play', 'square']);
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const XLINK_NS = 'http://www.w3.org/1999/xlink';
    const ELIGIBLE_SELECTOR = [
        '.nav-icon', '.sidebar-search-icon', '.sidebar-collapse-button', '.sidebar-new-chat',
        '.chat-send-button', '.placeholder-icon', '.hint-icon',
        '.component-icon', '.path-check-icon', '.path-check-status', '.config-status-icon',
        '.check-icon', '.port-status-icon', '.deploy-item', '.deploy-item-icon', '.memory-stat-card .stat-icon',
        '.memory-empty', '.memory-loading', '.memory-error', '.memory-maintain-section',
        '.db-stats-bar', '.db-filter-hint', '.knowledge-stats', '.knowledge-readonly-badge',
        '.config-item', '.terminal-tab', '.btn', '.btn-sm', '.btn-icon', '.btn-download', '.dd-check',
        '.setup-card-header', '.setup-hint-banner', '.form-section-title', '.setup-nav',
        '.path-check-result', '.editor-placeholder', '.editor-actions', '.plugin-actions', '.ui-settings'
    ].join(', ');
    const PRESERVE_SELECTOR = [
        'input', 'textarea', 'select', 'option', 'script', 'style', 'pre', 'code',
        '[contenteditable="true"]', '[data-preserve-emoji]', '#chat-history-panel',
        '.chat-messages', '.terminal-output', '.deploy-log', '#modal-overlay', '.ui-icon'
    ].join(', ');

    const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const ICON_TOKEN_RE = new RegExp(
        Object.keys(GLYPH_TO_ICON)
            .sort((a, b) => b.length - a.length)
            .map((token) => `${escapeRegExp(token)}\\uFE0F?`)
            .join('|'),
        'gu'
    );

    function normalizeToken(value) {
        return value.replace(/\uFE0F/g, '');
    }

    function createIcon(name, { className = '', label = '' } = {}) {
        const safeName = ICON_NAMES.has(name) ? name : 'circle-help';
        const icon = document.createElementNS(SVG_NS, 'svg');
        icon.setAttribute('viewBox', '0 0 24 24');
        icon.setAttribute('focusable', 'false');
        icon.setAttribute('class', `ui-icon icon-${safeName}${className ? ` ${className}` : ''}`);
        if (label) {
            icon.setAttribute('role', 'img');
            icon.setAttribute('aria-label', label);
        } else {
            icon.setAttribute('aria-hidden', 'true');
        }

        const use = document.createElementNS(SVG_NS, 'use');
        const href = `${SPRITE_URL}#${safeName}`;
        use.setAttribute('href', href);
        use.setAttributeNS(XLINK_NS, 'xlink:href', href);
        icon.appendChild(use);
        return icon;
    }

    // Public helper for any future component that needs a semantic SVG in a template literal.
    window.NachoIcon = (name, options) => createIcon(name, options).outerHTML;

    function canHydrate(textNode) {
        const parent = textNode.parentElement;
        if (!parent || parent.closest(PRESERVE_SELECTOR)) return false;
        return Boolean(parent.closest(ELIGIBLE_SELECTOR));
    }

    function placementClass(source, start, length) {
        const hasBefore = source.slice(0, start).trim().length > 0;
        const hasAfter = source.slice(start + length).trim().length > 0;
        if (!hasBefore && !hasAfter) return 'is-solo';
        if (!hasBefore) return 'is-leading';
        if (!hasAfter) return 'is-trailing';
        return 'is-middle';
    }

    function hydrateTextNode(textNode) {
        if (!canHydrate(textNode) || !textNode.nodeValue) return;

        const source = textNode.nodeValue;
        const matches = Array.from(source.matchAll(ICON_TOKEN_RE));
        if (!matches.length) return;

        const fragment = document.createDocumentFragment();
        let cursor = 0;
        for (const match of matches) {
            const start = match.index;
            if (start > cursor) fragment.appendChild(document.createTextNode(source.slice(cursor, start)));
            const name = GLYPH_TO_ICON[normalizeToken(match[0])];
            fragment.appendChild(createIcon(name, { className: placementClass(source, start, match[0].length) }));
            cursor = start + match[0].length;
        }
        if (cursor < source.length) fragment.appendChild(document.createTextNode(source.slice(cursor)));
        textNode.replaceWith(fragment);
    }

    function hydrateNode(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            hydrateTextNode(node);
            return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE || node.matches(PRESERVE_SELECTOR)) return;

        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach(hydrateTextNode);
    }

    function observeChanges() {
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                if (mutation.type === 'characterData') {
                    hydrateTextNode(mutation.target);
                    return;
                }
                mutation.addedNodes.forEach(hydrateNode);
            });
        });
        observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }

    function initialize() {
        hydrateNode(document.body);
        observeChanges();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, { once: true });
    } else {
        initialize();
    }
})();
