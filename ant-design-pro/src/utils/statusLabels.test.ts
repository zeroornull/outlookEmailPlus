import { describe, expect, it } from 'vitest';
import { accountStatusLabel, refreshStatusLabel } from './statusLabels';

describe('refreshStatusLabel', () => {
  it('shows pending credential validation separately from an in-progress refresh', () => {
    expect(refreshStatusLabel('pending')).toBe('待验证');
    expect(refreshStatusLabel('PENDING')).toBe('待验证');
    expect(refreshStatusLabel('running')).toBe('进行中');
  });

  it('preserves actual refresh results and the separate administrative status', () => {
    expect(refreshStatusLabel('success')).toBe('成功');
    expect(refreshStatusLabel('failed')).toBe('失败');
    expect(accountStatusLabel('pending')).toBe('待处理');
  });
});
