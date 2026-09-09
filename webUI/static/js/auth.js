/** WebUI bearer-token transport for HTTP and WebSocket control requests. */
(() => {
    const TOKEN_KEY = 'nachobot-webui-token';
    const nativeFetch = window.fetch.bind(window);
    let promptInFlight = null;

    function currentToken() {
        return sessionStorage.getItem(TOKEN_KEY) || '';
    }

    function requestToken() {
        if (!promptInFlight) {
            promptInFlight = Promise.resolve().then(() => {
                const value = window.prompt('请输入 NachoBot WebUI 访问令牌') || '';
                const token = value.trim();
                if (token) sessionStorage.setItem(TOKEN_KEY, token);
                return token;
            }).finally(() => {
                promptInFlight = null;
            });
        }
        return promptInFlight;
    }

    function sameOriginApi(input) {
        try {
            const raw = typeof input === 'string' ? input : input.url;
            const url = new URL(raw, location.href);
            return url.origin === location.origin && url.pathname.startsWith('/api');
        } catch (_) {
            return false;
        }
    }

    function authenticatedInit(init = {}, token = currentToken()) {
        const headers = new Headers(init.headers || {});
        if (token) headers.set('X-Nachobot-Token', token);
        return { ...init, headers };
    }

    window.fetch = async function webuiFetch(input, init = {}) {
        const isApi = sameOriginApi(input);
        let response = await nativeFetch(input, isApi ? authenticatedInit(init) : init);
        if (isApi && response.status === 401) {
            sessionStorage.removeItem(TOKEN_KEY);
            const token = await requestToken();
            if (token) response = await nativeFetch(input, authenticatedInit(init, token));
        }
        return response;
    };

    function base64Url(value) {
        const bytes = new TextEncoder().encode(value);
        let binary = '';
        bytes.forEach(byte => { binary += String.fromCharCode(byte); });
        return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
    }

    window.createAuthenticatedWebSocket = function createAuthenticatedWebSocket(url) {
        const protocols = ['nachobot'];
        const token = currentToken();
        if (token) protocols.push(`nachobot.auth.${base64Url(token)}`);
        return new WebSocket(url, protocols);
    };
})();
