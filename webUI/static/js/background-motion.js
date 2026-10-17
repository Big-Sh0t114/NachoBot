/**
 * Shared, user-controlled motion preference for the WebUI backgrounds.
 * This setting is intentionally independent of the browser's motion preference.
 */
(function () {
    'use strict';

    const SETTINGS_KEY = 'nacho_ui_settings';
    const subscribers = new Set();
    let reduced = false;

    function parseSettings(raw) {
        if (typeof raw !== 'string' || raw.length === 0) return {};
        try {
            const value = JSON.parse(raw);
            return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
        } catch (error) {
            return {};
        }
    }

    function readSettings() {
        try {
            return parseSettings(window.localStorage.getItem(SETTINGS_KEY));
        } catch (error) {
            return {};
        }
    }

    function notifySubscriber(subscriber) {
        try {
            subscriber(reduced);
        } catch (error) {
            console.error('[background-motion] Subscriber failed.', error);
        }
    }

    function notifySubscribers() {
        for (const subscriber of [...subscribers]) {
            notifySubscriber(subscriber);
        }
    }

    function applyStoredValue(raw) {
        const settings = parseSettings(raw);
        const nextReduced = settings.reducedMotion === true;
        if (reduced === nextReduced) return;
        reduced = nextReduced;
        notifySubscribers();
    }

    function setReduced(value) {
        const nextReduced = value === true;
        const changed = reduced !== nextReduced;
        reduced = nextReduced;

        try {
            const settings = readSettings();
            settings.reducedMotion = reduced;
            window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
        } catch (error) {
            // Keep the in-memory setting usable if localStorage is blocked.
        }

        if (changed) notifySubscribers();
        return reduced;
    }

    function subscribe(callback) {
        if (typeof callback !== 'function') return () => {};
        subscribers.add(callback);
        notifySubscriber(callback);
        return () => subscribers.delete(callback);
    }

    window.addEventListener('storage', (event) => {
        if (!event || event.key === SETTINGS_KEY || event.key === null) {
            applyStoredValue(event ? event.newValue : null);
        }
    });

    applyStoredValue((() => {
        try {
            return window.localStorage.getItem(SETTINGS_KEY);
        } catch (error) {
            return null;
        }
    })());

    window.BackgroundMotion = {
        isReduced: () => reduced,
        setReduced,
        subscribe,
    };
})();
