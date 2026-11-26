/** True only while the mailbox page itself is the current route. */
export function isMailboxRoute(pathname: string): boolean {
  return pathname === '/mailbox' || pathname.startsWith('/mailbox/');
}
