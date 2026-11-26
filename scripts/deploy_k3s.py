#!/usr/bin/env python3
"""Deploy an immutable image to k3s, backing up SQLite before changing it."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request


_BACKUP_CODE = """
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

source = Path(os.environ.get('DATABASE_PATH', 'data/outlook_accounts.db')).resolve()
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
target = source.with_name(source.name + '.' + stamp + '-' + uuid4().hex + '.bak')
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
    with target.open('xb'):
        pass
    try:
        with sqlite3.connect(str(target)) as backup:
            original.backup(backup)
    except Exception:
        target.unlink(missing_ok=True)
        raise
print(json.dumps({'path': str(target), 'bytes': target.stat().st_size}))
"""


class DeployError(Exception):
    pass


def run_kubectl(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ['kubectl', *args], capture_output=True, text=True,
            encoding='utf-8', errors='replace', check=False,
        )
    except FileNotFoundError as exc:
        raise DeployError('找不到 kubectl，请先安装并加入 PATH。') from exc
    except OSError as exc:
        raise DeployError(f'无法启动 kubectl：{exc}') from exc
    if result.returncode:
        detail = result.stderr.strip() or f'kubectl 退出码 {result.returncode}'
        raise DeployError(detail)
    return result.stdout.strip()


def container_image(resource_args: list[str], deployment: str, container: str) -> str:
    raw = run_kubectl([*resource_args, 'get', f'deployment/{deployment}', '-o', 'json'])
    try:
        document = json.loads(raw)
        containers = document['spec']['template']['spec']['containers']
        for item in containers:
            if item['name'] == container:
                image = item['image']
                if isinstance(image, str) and image:
                    return image
                break
    except (ValueError, KeyError, TypeError):
        raise DeployError('无法解析 Deployment 的容器信息（原始内容未输出）。') from None
    raise DeployError(f'Deployment 中未找到有效目标容器：{container}')


def check_health(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            if response.status != 200:
                raise DeployError(f'健康检查 HTTP 状态为 {response.status}，预期 200。')
            try:
                payload = json.load(response)
            except (ValueError, UnicodeError):
                raise DeployError('健康检查响应不是有效 JSON（响应正文未输出）。') from None
    except urllib.error.HTTPError as exc:
        raise DeployError(f'健康检查 HTTP 状态为 {exc.code}，预期 200。') from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise DeployError(f'健康检查请求失败：{exc}') from None
    if not isinstance(payload, dict) or payload.get('status') != 'ok':
        raise DeployError('健康检查 JSON.status 不等于 ok（响应正文未输出）。')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='完整 registry/name@sha256:<64位十六进制摘要>')
    parser.add_argument('--context', help='默认使用 kubectl 当前 context')
    parser.add_argument('--namespace', default='dev')
    parser.add_argument('--deployment', default='outlook-email-plus')
    parser.add_argument('--container', default='outlook-email-plus')
    parser.add_argument('--health-url', default='https://outlook-email-plus.173114.xyz/healthz')
    parser.add_argument('--timeout', default='180s', help='kubectl rollout 超时，默认 180s')
    parser.add_argument('--dry-run', action='store_true', help='仅执行服务端 dry-run，不备份、不部署、不检查 health')
    args = parser.parse_args()
    if not re.fullmatch(r'[^\s@]+/[^\s@:]+(?:/[^\s@:]+)*@sha256:[0-9a-fA-F]{64}', args.image):
        parser.error('--image 必须是完整固定摘要引用；请从 GitHub Actions 摘要的 Pinned image 复制，不支持可变 tag。')

    step = '读取 kubectl context'
    try:
        context = args.context or run_kubectl(['config', 'current-context'])
        if not context.strip():
            raise DeployError('kubectl 当前 context 为空。')
        print(f'context: {context}', flush=True)
        resource_args = ['--context', context, '--namespace', args.namespace]
        step = '查询当前 Deployment 与目标容器'
        previous = container_image(resource_args, args.deployment, args.container)
        print(f'namespace: {args.namespace}\ndeployment: {args.deployment}\ncontainer: {args.container}\n'
              f'previous image: {previous}\nnew image: {args.image}', flush=True)
        set_image = [*resource_args, 'set', 'image', f'deployment/{args.deployment}',
                     f'{args.container}={args.image}']
        if args.dry_run:
            step = '服务端 dry-run'
            print(run_kubectl([*set_image, '--dry-run=server', '-o', 'name']), flush=True)
            print('state: dry-run succeeded（未执行备份、部署或健康检查）', flush=True)
            return 0

        step = '在线备份 SQLite'
        raw_backup = run_kubectl([
            *resource_args, 'exec', f'deployment/{args.deployment}', '-c', args.container,
            '--', 'python', '-c', _BACKUP_CODE,
        ])
        try:
            backup = json.loads(raw_backup)
            backup_path = backup['path']
            backup_bytes = backup['bytes']
            if not isinstance(backup_path, str) or not isinstance(backup_bytes, int) or backup_bytes <= 0:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise DeployError('备份返回信息无效；尚未修改 Deployment，请检查容器数据库目录。') from None
        print(f'backup: {backup_path}\nbackup bytes: {backup_bytes}', flush=True)
        step = '更新 Deployment image'
        print(run_kubectl(set_image), flush=True)
        step = '等待 rollout'
        print(run_kubectl([*resource_args, 'rollout', 'status', f'deployment/{args.deployment}',
                           f'--timeout={args.timeout}']), flush=True)
        step = '核对部署 image'
        actual = container_image(resource_args, args.deployment, args.container)
        if actual != args.image:
            raise DeployError(f'部署 image 与请求不一致，实际为：{actual}')
        step = '检查公网 health'
        check_health(args.health_url)
        print(f'health: HTTP 200, status=ok\nstate: deployed\nimage: {actual}', flush=True)
        return 0
    except (DeployError, ValueError) as exc:
        print(f'失败步骤：{step}\n{exc}\nstate: failed（未自动回滚）', file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print(f'已中断，步骤：{step}\nstate: interrupted（未自动回滚）', file=sys.stderr, flush=True)
        return 130


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())
