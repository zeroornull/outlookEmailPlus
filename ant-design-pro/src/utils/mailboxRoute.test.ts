import { describe, expect, it } from 'vitest';
import { isMailboxRoute } from './mailboxRoute';

describe('isMailboxRoute', () => {
  it('accepts only the mailbox page', () => {
    expect(isMailboxRoute('/mailbox')).toBe(true);
    expect(isMailboxRoute('/mailbox/')).toBe(true);
    expect(isMailboxRoute('/accounts')).toBe(false);
    expect(isMailboxRoute('/mailbox-settings')).toBe(false);
  });
});
