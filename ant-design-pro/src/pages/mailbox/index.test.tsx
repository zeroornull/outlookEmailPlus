import React from 'react';
import { App } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AccountListParams } from '@/services/outlook/accounts';

const mocks = vi.hoisted(() => ({
  fetchAccounts: vi.fn(),
  fetchEmails: vi.fn(),
}));

vi.mock('@umijs/max', () => ({
  history: { location: { pathname: '/mailbox' }, replace: vi.fn() },
  useLocation: () => ({ search: '' }),
  useModel: () => ({ initialState: { currentUser: { userid: 'test' } } }),
  useIntl: () => ({ formatMessage: ({ defaultMessage }: { defaultMessage: string }) => defaultMessage }),
}));
vi.mock('@ant-design/pro-components', () => ({
  PageContainer: ({ children, extra }: React.PropsWithChildren<{ extra?: React.ReactNode }>) => <div>{extra}{children}</div>,
  ProCard: ({ children, title, extra }: React.PropsWithChildren<{ title?: React.ReactNode; extra?: React.ReactNode }>) => <div>{title}{extra}{children}</div>,
}));
// Keep the actual account controls, but leave the unrelated mail reader and resize machinery out.
vi.mock('@/components/MailboxLayout/ResizableWorkbench', () => ({
  default: ({ groups, accounts }: { groups: React.ReactNode; accounts: React.ReactNode }) => <div>{groups}{accounts}</div>,
}));
vi.mock('@/services/outlook/accounts', () => ({ fetchAccounts: mocks.fetchAccounts }));
vi.mock('@/services/outlook/groups', () => ({
  fetchGroups: async () => ({ success: true, groups: [{ id: 7, name: 'Work', account_count: 101 }, { id: 8, name: 'Other', account_count: 1 }] }),
  isTempMailboxGroup: () => false,
}));
vi.mock('@/services/outlook/emails', () => ({
  fetchEmails: mocks.fetchEmails,
  deleteEmails: vi.fn(),
  extractEmailVerification: vi.fn(),
  fetchEmailDetail: vi.fn(),
  normalizeMethodParam: (method: string) => method,
  pickEmailsErrorMessage: () => 'Mail error',
}));
vi.mock('@/services/outlook/pollEngine', () => ({
  getPollSettings: () => ({ enabled: false, interval: 10, maxCount: 3 }),
  loadPollSettingsFromServer: async () => ({ enabled: false, interval: 10, maxCount: 3 }),
  subscribePoll: () => () => {},
  getPollSnapshots: () => [],
  getPollSnapshot: () => undefined,
  isPolling: () => false,
  applyPollSettings: vi.fn(),
  startPoll: vi.fn(),
  stopPoll: vi.fn(),
}));

import MailboxPage from './index';

const accounts = Array.from({ length: 102 }, (_, index) => ({
  id: index + 1,
  email: index === 100 ? 'needle-work@example.com' : index === 101 ? 'needle-other@example.com' : `account-${index + 1}@example.com`,
  group_id: index === 101 ? 8 : 7,
  group_name: index === 101 ? 'Other' : 'Work',
  status: 'active',
  provider: 'outlook',
}));
let client: QueryClient;

function mount(mode: 'standard' | 'compact') {
  localStorage.setItem('ol_mailbox_view_mode', mode);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><App><MailboxPage /></App></QueryClientProvider>);
}

function searchInput() {
  return screen.getByPlaceholderText(/筛选账号|搜索账号/);
}

async function expectRequest(expected: Partial<AccountListParams>) {
  await waitFor(() => expect(mocks.fetchAccounts).toHaveBeenLastCalledWith(expect.objectContaining(expected)));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.fetchEmails.mockResolvedValue({ success: true, emails: [], has_more: false });
  mocks.fetchAccounts.mockImplementation(async (params: AccountListParams) => {
    const search = params.search?.trim().toLowerCase() || '';
    const matching = accounts.filter((account) =>
      (params.group_id == null || account.group_id === params.group_id) &&
      (!search || account.email.includes(search)),
    );
    const page = params.page || 1;
    const pageSize = Math.min(params.page_size || 100, 100);
    return {
      success: true,
      accounts: matching.slice((page - 1) * pageSize, page * pageSize),
      pagination: { page, page_size: pageSize, total_count: matching.length, total_pages: Math.ceil(matching.length / pageSize) },
    };
  });
});
afterEach(() => {
  cleanup();
  client?.clear();
});

describe.each(['standard', 'compact'] as const)('mailbox account search in %s view', (mode) => {
  it('searches all groups beyond the first 100 accounts as the user types', async () => {
    mount(mode);
    await screen.findByText('account-1@example.com');
    expect(screen.queryByText('needle-other@example.com')).not.toBeInTheDocument();
    fireEvent.change(searchInput(), { target: { value: '  needle-other  ' } });
    expect(await screen.findByText('needle-other@example.com')).toBeVisible();
    await expectRequest({ page: 1, page_size: 100, search: 'needle-other' });
    expect(mocks.fetchAccounts.mock.lastCall?.[0].group_id).toBeUndefined();
    expect(screen.queryByText('account-1@example.com')).not.toBeInTheDocument();
  });

  it('keeps a selected group scoped when submitting a search', async () => {
    mount(mode);
    await screen.findByText('account-1@example.com');
    fireEvent.click(
      mode === 'compact'
        ? screen.getByRole('radio', { name: 'Work (101)' })
        : screen.getByRole('menuitem', { name: /Work/ }),
    );
    await expectRequest({ group_id: 7, page: 1 });
    fireEvent.change(searchInput(), { target: { value: 'needle' } });
    fireEvent.keyDown(searchInput(), { key: 'Enter', code: 'Enter', charCode: 13, keyCode: 13 });
    expect(await screen.findByText('needle-work@example.com')).toBeVisible();
    await expectRequest({ group_id: 7, page: 1, search: 'needle' });
    expect(screen.queryByText('needle-other@example.com')).not.toBeInTheDocument();
  });

  it('pages beyond the first 100 accounts and resets to page one when searching and clearing', async () => {
    mount(mode);
    await screen.findByText('account-1@example.com');
    fireEvent.click(screen.getByTitle('2'));
    expect(await screen.findByText('needle-other@example.com')).toBeVisible();
    await expectRequest({ page: 2, page_size: 100 });
    fireEvent.change(searchInput(), { target: { value: 'needle-other' } });
    fireEvent.keyDown(searchInput(), { key: 'Enter', code: 'Enter', charCode: 13, keyCode: 13 });
    await expectRequest({ page: 1, search: 'needle-other' });
    fireEvent.change(searchInput(), { target: { value: '' } });
    expect(await screen.findByText('account-1@example.com')).toBeVisible();
    expect(searchInput()).toHaveValue('');
    expect(screen.queryByText('needle-other@example.com')).not.toBeInTheDocument();
  });
});

it('shares the current search value and results when changing views', async () => {
  mount('standard');
  await screen.findByText('account-1@example.com');
  fireEvent.change(searchInput(), { target: { value: 'needle-other' } });
  await screen.findByText('needle-other@example.com');
  fireEvent.click(screen.getByText('简洁'));
  expect(searchInput()).toHaveValue('needle-other');
  expect(await screen.findByText('needle-other@example.com')).toBeVisible();
  await expectRequest({ page: 1, search: 'needle-other' });
});
