import { Typography } from 'antd';
import React from 'react';

const guideSteps = [
  {
    title: '注册应用',
    content: (
      <>
        <p>
          进入{' '}
          <a
            href="https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
            target="_blank"
            rel="noopener noreferrer"
          >
            Azure 门户 → 应用注册
          </a>
          ，点击「新注册」。
        </p>
        <ul>
          <li>名称：自定义（如 &quot;OutlookEmailPlus&quot;）</li>
          <li>
            受支持的账户类型：选择「
            <strong>
              Accounts in any organizational directory and personal Microsoft
              accounts
            </strong>
            」（任意目录 + 个人账号）
          </li>
          <li>
            工具仅支持个人 Microsoft 账号，授权使用 tenant=consumers。
          </li>
        </ul>
      </>
    ),
  },
  {
    title: '选择客户端模式',
    content: (
      <>
        <p>
          Web 应用：在「证书和密码」创建客户端密码，将密码值与应用 ID 配对配置为服务器的 OAUTH_CLIENT_SECRET / OAUTH_CLIENT_ID。
        </p>
        <Typography.Text type="secondary">
          公共客户端：可不配置 secret；仅此模式按 Azure 要求开启「允许公共客户端流」。Web 应用无需开启此选项，secret 不会发送到浏览器。
        </Typography.Text>
      </>
    ),
  },
  {
    title: '配置 API 权限',
    content: (
      <>
        <p>
          进入应用 →「API 权限」→「添加权限」→ 选择「Microsoft Graph」→「委托的权限」，添加以下权限：
        </p>
        <ul>
          <li>
            <code>offline_access</code>（必须，用于获取 refresh_token）
          </li>
          <li>
            <code>Mail.Read</code>（读取邮件）
          </li>
          <li>
            <code>User.Read</code>（读取用户信息）
          </li>
        </ul>
        <Typography.Text type="secondary">
          💡 推荐使用 Graph 协议（本系统默认邮件读取主链路），直接使用「Graph 邮件」预设即可。
        </Typography.Text>
      </>
    ),
  },
  {
    title: '配置重定向 URI',
    content: (
      <>
        <p>
          在「身份验证」页面选择与客户端模式对应的平台，并将同一地址填入下方 Redirect URI。
        </p>
        <ul>
          <li>
            Web：使用「Web」平台，远程部署填写本服务的 HTTPS 回调地址，
            如 <code>https://example.com/token-tool/callback</code>；本地可使用实际 localhost 回调地址。
          </li>
          <li>
            公共客户端：使用「Mobile and desktop applications」平台的 public redirect，
            如 <code>http://localhost</code>，授权后手动复制地址栏 URL 粘贴回本工具。
          </li>
        </ul>
      </>
    ),
  },
  {
    title: '获取 Client ID',
    content: (
      <p>
        回到应用「概述」页面，复制「应用程序(客户端) ID」，粘贴到下方 OAuth 配置的 Client ID
        输入框中。
      </p>
    ),
  },
];

const TokenToolGuide: React.FC = () => {
  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 16 }}>
        如果你是第一次使用，请先在 Azure 门户注册一个应用，获取 Client ID 后再填写下方配置。
      </Typography.Paragraph>
      {guideSteps.map((step, index) => (
        <div
          key={step.title}
          style={{
            display: 'flex',
            gap: 12,
            marginBottom: 16,
          }}
        >
          <div
            style={{
              width: 24,
              height: 24,
              borderRadius: '50%',
              background: 'rgba(184, 92, 56, 0.12)',
              color: '#B85C38',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 12,
              fontWeight: 600,
              flexShrink: 0,
            }}
          >
            {index + 1}
          </div>
          <div style={{ minWidth: 0 }}>
            <Typography.Text strong>{step.title}</Typography.Text>
            <div style={{ marginTop: 4 }}>{step.content}</div>
          </div>
        </div>
      ))}
      <Typography.Paragraph style={{ marginBottom: 0 }}>
        📖 详细教程：{' '}
        <a
          href="https://real-caption-6d1.notion.site/OutlooKMailplus-token-344463aed7e680099380dc324ecdf1c9?source=copy_link"
          target="_blank"
          rel="noopener noreferrer"
        >
          OutlookMailPlus Token 教程
        </a>
      </Typography.Paragraph>
    </div>
  );
};

export default TokenToolGuide;
