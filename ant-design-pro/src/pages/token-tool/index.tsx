import { PageContainer, ProCard } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Checkbox,
  Collapse,
  Form,
  Input,
  Space,
  Typography,
} from 'antd';
import React, { useEffect, useState } from 'react';
import TokenToolGuide from './TokenToolGuide';
import {
  exchangeToken,
  fetchTokenToolConfig,
  pickTokenToolError,
  prepareOAuth,
  saveTokenToolConfig,
  type TokenResult,
  type TokenToolConfig,
} from '@/services/outlook/tokenTool';
import { useIntl } from '@umijs/max';

const DEFAULT_SCOPE =
  'https://graph.microsoft.com/Mail.Read offline_access openid profile email';

const SCOPE_PRESETS = {
  graph: 'offline_access openid profile email https://graph.microsoft.com/.default',
  imap: 'offline_access openid profile email https://outlook.office.com/IMAP.AccessAsUser.All',
} as const;

const TokenToolPage: React.FC = () => {
  const { message } = App.useApp();
  const intl = useIntl();
  const [configForm] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [configLoading, setConfigLoading] = useState(false);
  const [authorizeUrl, setAuthorizeUrl] = useState('');
  const [callbackUrl, setCallbackUrl] = useState('');
  const [tokenResult, setTokenResult] = useState<TokenResult | null>(null);

  const loadConfig = async () => {
    setConfigLoading(true);
    try {
      const res = await fetchTokenToolConfig();
      if (res?.success === false) {
        message.error(pickTokenToolError(res, '加载配置失败'));
        return;
      }
      const cfg = res.data || {};
      configForm.setFieldsValue({
        client_id: cfg.client_id || '',
        redirect_uri:
          cfg.redirect_uri || `${window.location.origin}/token-tool/callback`,
        scope: cfg.scope || DEFAULT_SCOPE,
        prompt_consent: !!cfg.prompt_consent,
      });
    } catch (error: unknown) {
      let responseData: unknown;
      if (error && typeof error === 'object' && 'response' in error) {
        const response = error.response;
        if (response && typeof response === 'object' && 'data' in response) {
          responseData = response.data;
        }
      }
      message.error(
        pickTokenToolError(
          responseData,
          error instanceof Error ? error.message : '加载配置失败',
        ),
      );
    } finally {
      setConfigLoading(false);
    }
  };

  useEffect(() => {
    void loadConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const collectConfig = (): TokenToolConfig => {
    const values = configForm.getFieldsValue();
    return {
      client_id: values.client_id?.trim() || '',
      client_secret: '',
      redirect_uri: values.redirect_uri?.trim() || '',
      scope: values.scope?.trim() || DEFAULT_SCOPE,
      tenant: 'consumers',
      prompt_consent: !!values.prompt_consent,
    };
  };

  const onSaveConfig = async () => {
    try {
      const res = await saveTokenToolConfig(collectConfig());
      if (res?.success === false) {
        message.error(pickTokenToolError(res, '保存失败'));
        return;
      }
      message.success(res.message || '配置已保存');
    } catch (error: any) {
      message.error(
        pickTokenToolError(error?.response?.data, error?.message || '保存失败'),
      );
    }
  };

  const onPrepare = async () => {
    setLoading(true);
    try {
      const res = await prepareOAuth(collectConfig());
      if (res?.success === false) {
        message.error(pickTokenToolError(res, '生成授权链接失败'));
        return;
      }
      const url = res.data?.authorize_url || '';
      setAuthorizeUrl(url);
      if (!url) {
        message.error('授权地址为空');
        return;
      }
      try {
        await navigator.clipboard.writeText(url);
        message.success('授权链接已生成并复制');
      } catch {
        message.warning('授权链接已生成，自动复制失败，请点击「复制」或手动复制链接');
      }
    } catch (error: any) {
      message.error(
        pickTokenToolError(error?.response?.data, error?.message || '生成失败'),
      );
    } finally {
      setLoading(false);
    }
  };

  const onExchange = async () => {
    if (!callbackUrl.trim()) {
      message.error('请粘贴回调 URL');
      return;
    }
    setLoading(true);
    try {
      const res = await exchangeToken(callbackUrl.trim());
      if (res?.success === false) {
        message.error(pickTokenToolError(res, '换取 Token 失败'));
        return;
      }
      const result = res.data || {};
      setTokenResult(result);
      if (!result?.import_error && result?.account?.email &&
          (result.account.action === 'created' || result.account.action === 'updated')) {
        message.success(`${result.account.email}：${result.account.action === 'created' ? '账号已创建，Token 已导入' : '账号 Token 已更新'}`);
      }
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : '换取 Token 失败，请重新授权后重试');
    } finally {
      setLoading(false);
    }
  };


  return (
    <PageContainer
      title={intl.formatMessage({
        id: 'outlook.tokenTool.title',
        defaultMessage: 'OAuth Token 工具',
      })}
      subTitle={intl.formatMessage({
        id: 'outlook.tokenTool.subtitle',
        defaultMessage: '获取与刷新邮箱授权令牌',
      })}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Collapse
          defaultActiveKey={['guide']}
          items={[
            {
              key: 'guide',
              label: 'Azure 应用注册快速指引',
              children: <TokenToolGuide />,
            },
          ]}
        />

        <ProCard title="OAuth 配置" variant="outlined" loading={configLoading}>
          <Form
            form={configForm}
            layout="vertical"
            initialValues={{ scope: DEFAULT_SCOPE }}
          >
            <Form.Item
              name="client_id"
              label="Client ID"
              rules={[{ required: true, message: '请输入 Client ID' }]}
            >
              <Input />
            </Form.Item>
            <Form.Item name="redirect_uri" label="Redirect URI">
              <Input />
            </Form.Item>
            <Form.Item name="scope" label="Scope">
              <Input.TextArea rows={2} />
            </Form.Item>
            <Space wrap style={{ marginBottom: 16 }}>
              <Typography.Text type="secondary">预设：</Typography.Text>
              <Button
                size="small"
                onClick={() =>
                  configForm.setFieldValue('scope', SCOPE_PRESETS.graph)
                }
              >
                Graph 邮件
              </Button>
              <Button
                size="small"
                onClick={() =>
                  configForm.setFieldValue('scope', SCOPE_PRESETS.imap)
                }
              >
                IMAP
              </Button>
            </Space>
            <Typography.Paragraph type="secondary" style={{ marginTop: -8 }}>
              推荐优先使用 Graph 邮件预设（系统默认邮件读取主链路）；如需 IMAP
              协议请切换至 IMAP 预设。
              仅支持个人 Microsoft 账号（tenant=consumers）。Web 应用由服务器按 Client ID
              匹配 OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET；公共客户端可不配置 secret。
            </Typography.Paragraph>
            <Form.Item
              name="prompt_consent"
              valuePropName="checked"
              label="强制同意提示"
            >
              <Checkbox>prompt=consent</Checkbox>
            </Form.Item>
            <Space wrap>
              <Button onClick={() => void onSaveConfig()}>保存配置</Button>
              <Button type="primary" loading={loading} onClick={() => void onPrepare()}>
                生成并复制授权链接
              </Button>
            </Space>
          </Form>
        </ProCard>

        {authorizeUrl ? (
          <ProCard title="授权链接" variant="outlined">
            <Input.TextArea rows={3} value={authorizeUrl} readOnly />
            <Space style={{ marginTop: 12 }}>
              <Button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(authorizeUrl);
                    message.success('已复制');
                  } catch {
                    message.error('复制失败，请选中授权链接后手动复制');
                  }
                }}
              >
                复制
              </Button>
              <Button type="primary" href={authorizeUrl} target="_blank">
                打开
              </Button>
            </Space>
          </ProCard>
        ) : null}

        <ProCard title="粘贴回调 URL 换取 Token 并导入" variant="outlined">
          <Input.TextArea
            rows={3}
            aria-label="回调 URL"
            placeholder="将浏览器地址栏的回调 URL 粘贴到这里"
            value={callbackUrl}
            onChange={(e) => setCallbackUrl(e.target.value)}
          />
          <Button
            type="primary"
            style={{ marginTop: 12 }}
            loading={loading}
            onClick={() => void onExchange()}
          >
            换取 Token 并导入
          </Button>
        </ProCard>
        <div role="status" aria-live="polite" aria-atomic="true">
          {tokenResult ? (
            tokenResult.import_error || !tokenResult.account?.email ||
            !['created', 'updated'].includes(tokenResult.account.action) ? (
              <Alert
                type="error"
                showIcon
                message="Token 已获取，但未导入账号"
                description={`${tokenResult.import_error?.message || '服务器未确认账号导入结果'}${tokenResult.import_error?.details ? `：${tokenResult.import_error.details}` : ''}。请先复制并妥善保管下方 Token，检查服务器配置和账号导入错误后，重新生成授权链接并授权导入。`}
              />
            ) : (
              <Alert
                type="success"
                showIcon
                message={`${tokenResult.account.email}：${tokenResult.account.action === 'created' ? '账号已创建，Token 已导入' : '账号 Token 已更新'}`}
              />
            )
          ) : null}
        </div>

        {tokenResult ? (
          <ProCard title="Token 结果" variant="outlined">
            {(
              [
                ['refresh_token', tokenResult.refresh_token],
                ['access_token', tokenResult.access_token],
                ['client_id', tokenResult.client_id],
                ['granted_scope', tokenResult.granted_scope],
                ['expires_in', String(tokenResult.expires_in || '')],
              ] as Array<[string, string | undefined]>
            ).map(([key, value]) => (
              <div key={key} style={{ marginBottom: 8 }}>
                <Typography.Text type="secondary">{key}</Typography.Text>
                <Input.TextArea
                  rows={key.includes('token') ? 2 : 1}
                  aria-label={key}
                  onFocus={(event) => event.target.select()}
                  value={value || ''}
                  readOnly
                />
                <Button
                  size="small"
                  aria-label={`复制 ${key}`}
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(value || '');
                      message.success('已复制');
                    } catch {
                      message.error('复制失败，请选中 Token 后手动复制');
                    }
                  }}
                >
                  复制
                </Button>
              </div>
            ))}
          </ProCard>
        ) : null}
      </Space>

    </PageContainer>
  );
};

export default TokenToolPage;
