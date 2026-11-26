import { describe, expect, it } from 'vitest';
import { accountStatusLabel, healthStatusColor, healthStatusLabel, refreshStatusLabel } from './statusLabels';

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

describe('healthStatusLabel', () => {
  it.each([
    ['active', '正常', 'success'],
    ['disabled', '停用', 'default'],
    ['expired', '授权过期', 'error'],
    ['error', '刷新异常', 'error'],
    ['pending_refresh', '待验证', 'gold'],
  ])('displays health %s with its matching color', (health, label, color) => {
    expect(healthStatusLabel(health)).toBe(label);
    expect(healthStatusColor(health)).toBe(color);
  });

  it.each(['inactive', 'pending', 'unknown', '', null, undefined])(
    'falls back to the administrative label for %s',
    (health) => {
      expect(healthStatusLabel(health)).toBe(accountStatusLabel(health));
    },
  );
});
