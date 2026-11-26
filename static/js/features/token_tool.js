// ==================== Token 工具 ====================

const t = window.translateAppText || ((text) => text);

// ===== 新手指引配置 =====
// 教程链接列表 — 在此维护外部教程链接，HTML 中 guide-links 区域会自动渲染
const GUIDE_TUTORIAL_LINKS = [
    // { title: '标题', url: 'https://example.com/tutorial' },
    // 在此添加教程链接...
];

const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || '';
const SCOPE_PRESETS = {
    graph: ['offline_access', 'openid', 'profile', 'email', 'https://graph.microsoft.com/.default'],
    imap: ['offline_access', 'openid', 'profile', 'email', 'https://outlook.office.com/IMAP.AccessAsUser.All'],
};
const DEFAULT_COMPAT_SCOPE = SCOPE_PRESETS.graph.join(' ');

let scopeTokens = [...SCOPE_PRESETS.graph];
let currentTokenResult = null;

async function tokenToolFetch(url, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        'X-CSRFToken': CSRF_TOKEN,
        ...(options.headers || {}),
    };
    const response = await fetch(url, { ...options, headers });
    const data = await response.json().catch(() => ({
        success: false,
        error: { message: t('响应解析失败') },
    }));
    if (!response.ok && data.success === undefined) {
        data.success = false;
    }
    return data;
}

function escapeHtml(value) {
    return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function buildDefaultRedirectUri() {
    return `${window.location.origin}/token-tool/callback`;
}

function showStatus(message, type = 'info', detail = '') {
    const statusNode = document.getElementById('statusMessage');
    if (!statusNode) {
        return;
    }
    statusNode.className = `token-status ${type}`;
    statusNode.innerHTML = `
        <div class="token-status-title">${escapeHtml(message)}</div>
        ${detail ? `<div class="token-status-detail">${escapeHtml(detail)}</div>` : ''}
    `;
}

function clearStatus() {
    const statusNode = document.getElementById('statusMessage');
    if (!statusNode) {
        return;
    }
    statusNode.className = 'token-status hidden';
    statusNode.innerHTML = '';
}


function parseScopeInput(raw) {
    return String(raw || '')
        .split(/[\s,;]+/)
        .map(item => item.trim())
        .filter(Boolean);
}

function updateScopeValue() {
    const scopeValue = document.getElementById('scopeValue');
    if (scopeValue) {
        scopeValue.value = scopeTokens.join(' ');
    }
}

function buildScopeChip(token) {
    const locked = token === 'offline_access';
    const chip = document.createElement('span');
    chip.className = locked ? 'scope-chip scope-chip-locked' : 'scope-chip';

    const label = document.createElement('span');
    label.textContent = token;
    chip.appendChild(label);

    if (locked) {
        const lock = document.createElement('span');
        lock.className = 'scope-chip-lock';
        lock.textContent = '🔒';
        chip.appendChild(lock);
        return chip;
    }

    const removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.dataset.scope = token;
    removeButton.setAttribute('aria-label', t('移除 scope') + ' ' + token);
    removeButton.textContent = '×';
    chip.appendChild(removeButton);
    return chip;
}

function renderScopeChips(scopeValue) {
    const tokens = parseScopeInput(scopeValue);
    const unique = new Set(tokens);
    unique.add('offline_access');
    scopeTokens = Array.from(unique);
    updateScopeValue();

    const chipsNode = document.getElementById('scopeChips');
    if (!chipsNode) {
        return;
    }
    chipsNode.innerHTML = '';
    scopeTokens.forEach(token => {
        chipsNode.appendChild(buildScopeChip(token));
    });
}

function addScopeTokens(tokens) {
    if (!Array.isArray(tokens) || tokens.length === 0) {
        return;
    }
    renderScopeChips([...scopeTokens, ...tokens].join(' '));
}

function addScopeFromInput() {
    const scopeEntry = document.getElementById('scopeEntry');
    if (!scopeEntry) {
        return;
    }
    const tokens = parseScopeInput(scopeEntry.value);
    if (!tokens.length) {
        showStatus(t('请输入要添加的 scope'), 'error');
        return;
    }
    addScopeTokens(tokens);
    scopeEntry.value = '';
    clearStatus();
}

function removeScope(scope) {
    if (scope === 'offline_access') {
        return;
    }
    scopeTokens = scopeTokens.filter(item => item !== scope);
    renderScopeChips(scopeTokens.join(' '));
}

function handleScopeChipClick(event) {
    if (!(event.target instanceof Element)) {
        return;
    }
    const removeButton = event.target.closest('button[data-scope]');
    if (!removeButton) {
        return;
    }
    removeScope(removeButton.dataset.scope || '');
}

function setScopePreset(type) {
    const preset = SCOPE_PRESETS[type];
    if (!preset) {
        return;
    }
    renderScopeChips(preset.join(' '));
    clearStatus();
}

function handleTenantChange() {
    // No-op: tenant is hardcoded to 'consumers' on the backend.
}

function collectFormConfig() {
    return {
        client_id: document.getElementById('clientId')?.value.trim() || '',
        client_secret: '',
        redirect_uri: document.getElementById('redirectUri')?.value.trim() || '',
        scope: document.getElementById('scopeValue')?.value.trim() || '',
        tenant: 'consumers',
        prompt_consent: Boolean(document.getElementById('promptConsent')?.checked),
    };
}

async function loadOAuthConfig() {
    const data = await tokenToolFetch('/api/token-tool/config');
    if (!data.success) {
        showStatus(data.error?.message || t('加载配置失败'), 'error');
        return;
    }

    const config = data.data || {};
    document.getElementById('clientId').value = config.client_id || '';
    document.getElementById('redirectUri').value = config.redirect_uri || buildDefaultRedirectUri();
    document.getElementById('promptConsent').checked = Boolean(config.prompt_consent);

    handleTenantChange();
    renderScopeChips(config.scope || DEFAULT_COMPAT_SCOPE);
    clearStatus();
}

async function startOAuth() {
    clearStatus();
    const config = collectFormConfig();
    const data = await tokenToolFetch('/api/token-tool/prepare', {
        method: 'POST',
        body: JSON.stringify(config),
    });
    if (!data.success) {
        showStatus(data.error?.message || t('生成授权 URL 失败'), 'error');
        return;
    }

    const authorizeUrl = data.data?.authorize_url;
    if (!authorizeUrl) {
        showStatus(t('授权地址为空'), 'error');
        return;
    }

    // Display the authorize link in the panel
    const linkInput = document.getElementById('authorizeUrl');
    if (linkInput) {
        linkInput.value = authorizeUrl;
    }
    const panel = document.getElementById('authorize-link-panel');
    if (panel) {
        panel.classList.remove('hidden');
    }
    document.getElementById('manual-exchange').open = true;
    showStatus(t('授权链接已生成，请复制并在浏览器中打开'), 'success');
}

function copyAuthorizeLink() {
    const linkInput = document.getElementById('authorizeUrl');
    if (!linkInput || !linkInput.value) {
        showStatus(t('没有可复制的授权链接'), 'error');
        return;
    }
    copyText(linkInput.value);
}

function openAuthorizeLink() {
    const linkInput = document.getElementById('authorizeUrl');
    if (!linkInput || !linkInput.value) {
        showStatus(t('没有可打开的授权链接'), 'error');
        return;
    }
    window.open(linkInput.value, '_blank');
}

function fillResultField(id, value) {
    const node = document.getElementById(id);
    if (node) {
        node.value = value || '';
    }
}

function renderTokenResult(result) {
    currentTokenResult = result || {};
    document.getElementById('result-panel').classList.remove('hidden');
    const imported = !result.import_error && result.account?.email &&
        ['created', 'updated'].includes(result.account.action);
    const banner = document.getElementById('resultImportStatus');
    banner.className = `token-status ${imported ? 'success' : 'error'}`;
    banner.textContent = imported
        ? `${result.account.email}：${result.account.action === 'created' ? t('账号已创建，Token 已导入') : t('账号 Token 已更新')}`
        : `${t('Token 已获取，但未导入账号')}：${result.import_error?.message || t('服务器未确认账号导入结果')}${result.import_error?.details ? `；${result.import_error.details}` : ''}。${t('请先复制并妥善保管下方 Token，检查服务器配置和账号导入错误后，重新生成授权链接并授权导入。')}`;

    fillResultField('refreshTokenResult', result.refresh_token || '');
    fillResultField('accessTokenResult', result.access_token || '');
    fillResultField('clientIdResult', result.client_id || '');
    fillResultField('redirectUriResult', result.redirect_uri || '');
    fillResultField('requestedScopeResult', result.requested_scope || '');
    fillResultField('grantedScopeResult', result.granted_scope || '');
    fillResultField('audienceResult', result.audience || '');
    fillResultField('scopeClaimResult', result.scope_claim || '');
    fillResultField('rolesClaimResult', result.roles_claim || '');
    fillResultField('expiresInResult', String(result.expires_in || ''));

}

function getCurrentTokenResult() {
    return currentTokenResult || {};
}

async function exchangeToken() {
    clearStatus();
    const callbackUrl = document.getElementById('callbackUrl')?.value.trim() || '';
    if (!callbackUrl) {
        showStatus(t('请粘贴回调 URL'), 'error');
        return;
    }

    const data = await tokenToolFetch('/api/token-tool/exchange', {
        method: 'POST',
        body: JSON.stringify({ callback_url: callbackUrl }),
    });
    if (!data.success) {
        showStatus(data.error?.message || t('换取 Token 失败'), 'error', data.error?.details || '');
        return;
    }
    renderTokenResult(data.data || {});
}

async function saveConfig() {
    clearStatus();
    const config = collectFormConfig();
    const data = await tokenToolFetch('/api/token-tool/config', {
        method: 'POST',
        body: JSON.stringify(config),
    });
    if (!data.success) {
        showStatus(data.error?.message || t('保存配置失败'), 'error');
        return;
    }
    showStatus(data.message || t('配置已保存'), 'success');
}

function copyResultField(id) {
    const node = document.getElementById(id);
    if (!node) {
        return;
    }
    copyText(node.value || '');
}

function copyAllResults() {
    const result = getCurrentTokenResult();
    const lines = [
        `refresh_token=${result.refresh_token || ''}`,
        `access_token=${result.access_token || ''}`,
        `client_id=${result.client_id || ''}`,
        `redirect_uri=${result.redirect_uri || ''}`,
        `requested_scope=${result.requested_scope || ''}`,
        `granted_scope=${result.granted_scope || ''}`,
        `audience=${result.audience || ''}`,
        `scope_claim=${result.scope_claim || ''}`,
        `roles_claim=${result.roles_claim || ''}`,
        `expires_in=${result.expires_in || ''}`,
    ];
    copyText(lines.join('\n'));
}

function copyText(text) {
    navigator.clipboard.writeText(text || '').then(() => {
        showStatus(t('内容已复制到剪贴板'), 'success');
    }).catch(() => {
        showStatus(t('复制失败，请手动复制'), 'error');
    });
}


document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('scopeChips')?.addEventListener('click', handleScopeChipClick);
    document.getElementById('redirectUri').value = buildDefaultRedirectUri();
    renderScopeChips(SCOPE_PRESETS.graph.join(' '));
    loadOAuthConfig();
    handleTenantChange();

    // 指引折叠状态记忆
    const guideCard = document.getElementById('guide-card');
    if (guideCard) {
        const guideDismissed = localStorage.getItem('token_tool_guide_dismissed');
        if (guideDismissed === 'true') {
            guideCard.removeAttribute('open');
        }
        guideCard.addEventListener('toggle', () => {
            localStorage.setItem('token_tool_guide_dismissed', guideCard.open ? '' : 'true');
        });
    }

    // 自动渲染教程链接到 guide-links 区域
    const guideLinksContainer = document.querySelector('.guide-links');
    if (guideLinksContainer && GUIDE_TUTORIAL_LINKS.length > 0) {
        GUIDE_TUTORIAL_LINKS.forEach((link) => {
            const a = document.createElement('a');
            a.href = link.url;
            a.target = '_blank';
            a.rel = 'noopener';
            a.textContent = link.title;
            guideLinksContainer.appendChild(a);
        });
    }
});
