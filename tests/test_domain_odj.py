"""Offline Domain Join provisioning: blob extraction, argv shape, soft failures.

Provisioning must never raise or fail a deploy — the caller falls back to late
Add-Computer — and the join password must never reach the argv (process list).
"""
import base64
import json
import socket
import subprocess

from app import app as flask_app
from app.domain_odj import _ensure_resolvable, provision_odj_blob


BLOB = 'QUJDREVGRw==' + 'A' * 80
DC_FQDN = 'WIN-DC01.lab.test'
ADS_INFO = (
    'LDAP server: 192.168.123.191\n'
    f'LDAP server name: {DC_FQDN}\n'
    'Realm: LAB.TEST\n'
)


def _resolves(*_a, **_k):
    """A getaddrinfo result shaped like the real one.

    `socket` is a shared module, so a stub here is also seen by
    `domain_preflight._collect_join_targets`.
    """
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.123.191', 0))]


def _unresolvable(*_a, **_k):
    raise socket.gaierror('name or service not known')


def _data(**over):
    data = {
        'join_domain': True,
        'domain_name': 'lab.test',
        'domain_username': 'administrator@lab.test',
        'domain_password': 'ChangeMe123!',
        'dns_servers': '192.168.123.191',
    }
    data.update(over)
    return data


class _Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch(monkeypatch, *, exists=True, result=None, raises=None, calls=None):
    """Stub out the two `net` calls; the DC FQDN is assumed already resolvable."""
    monkeypatch.setattr('app.domain_odj.os.path.exists', lambda p: exists)
    monkeypatch.setattr('app.domain_odj.socket.getaddrinfo', _resolves)

    def fake_run(argv, **kwargs):
        if 'info' in argv:
            return _Result(0, ADS_INFO)
        if calls is not None:
            calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return result if result is not None else _Result(0, BLOB)

    monkeypatch.setattr('app.domain_odj.subprocess.run', fake_run)


def _run(data, hostname='WIN-01', enabled=True):
    with flask_app.app_context():
        flask_app.config['DOMAIN_JOIN_ODJ'] = enabled
        return provision_odj_blob(data, hostname)


def test_returns_blob_and_keeps_password_out_of_argv(monkeypatch):
    calls = []
    _patch(monkeypatch, calls=calls)
    assert _run(_data(domain_ou='OU=Computers,DC=lab,DC=test')) == BLOB

    argv, kwargs = calls[0]
    joined = ' '.join(argv)
    assert 'ChangeMe123!' not in joined
    assert kwargs['env']['PASSWD'] == 'ChangeMe123!'
    assert 'offlinejoin' in argv and 'provision' in argv and 'printblob' in argv
    assert 'domain=lab.test' in argv
    assert 'machine_name=WIN-01' in argv
    assert 'machine_account_ou=OU=Computers,DC=lab,DC=test' in argv
    # Samba reconnects to the DC's own FQDN, so we target it directly.
    assert f'dcname={DC_FQDN}' in argv
    # UPN is split so Samba gets the bare account name.
    assert argv[argv.index('-U') + 1] == 'administrator'


def test_downlevel_username_is_split(monkeypatch):
    calls = []
    _patch(monkeypatch, calls=calls)
    assert _run(_data(domain_username='LAB\\svc-join')) == BLOB
    argv, _ = calls[0]
    assert argv[argv.index('-U') + 1] == 'svc-join'


def test_password_is_unpacked_from_join_blob(monkeypatch):
    """_prepare_domain_join pops the raw password once it is packed."""
    calls = []
    _patch(monkeypatch, calls=calls)
    packed = base64.b64encode(
        json.dumps(
            {
                'domain': 'lab.test',
                'username': 'administrator@lab.test',
                'password': 'FromBlob1!',
                'ou': 'OU=Servers,DC=lab,DC=test',
            }
        ).encode()
    ).decode()
    data = _data()
    data.pop('domain_password')
    data['domain_join_b64'] = packed

    assert _run(data) == BLOB
    argv, kwargs = calls[0]
    assert kwargs['env']['PASSWD'] == 'FromBlob1!'
    assert 'machine_account_ou=OU=Servers,DC=lab,DC=test' in argv


def test_disabled_by_config(monkeypatch):
    _patch(monkeypatch)
    assert _run(_data(), enabled=False) is None


def test_no_join_requested(monkeypatch):
    _patch(monkeypatch)
    assert _run(_data(join_domain=False)) is None


def test_missing_net_binary(monkeypatch):
    _patch(monkeypatch, exists=False)
    assert _run(_data()) is None


def test_nonzero_exit_falls_back(monkeypatch):
    _patch(monkeypatch, result=_Result(255, '', 'Failed to join domain: NT_STATUS_ACCESS_DENIED'))
    assert _run(_data()) is None


def test_zero_exit_without_blob_falls_back(monkeypatch):
    _patch(monkeypatch, result=_Result(0, 'Joined domain OK\n'))
    assert _run(_data()) is None


def test_timeout_falls_back(monkeypatch):
    _patch(monkeypatch, raises=subprocess.TimeoutExpired(cmd='net', timeout=60))
    assert _run(_data()) is None


def test_oserror_falls_back(monkeypatch):
    _patch(monkeypatch, raises=OSError('boom'))
    assert _run(_data()) is None


def test_second_dc_target_is_tried(monkeypatch):
    calls = []
    monkeypatch.setattr('app.domain_odj.os.path.exists', lambda p: True)
    monkeypatch.setattr('app.domain_odj.socket.getaddrinfo', _resolves)

    def fake_run(argv, **kwargs):
        if 'info' in argv:
            # Each DC reports a distinct FQDN.
            host = 'dc1' if '192.168.123.191' in argv else 'dc2'
            return _Result(0, f'LDAP server name: {host}.lab.test\n')
        calls.append(argv)
        if 'dcname=dc1.lab.test' in argv:
            return _Result(1, '', 'cannot contact')
        return _Result(0, BLOB)

    monkeypatch.setattr('app.domain_odj.subprocess.run', fake_run)
    data = _data(dns_servers='192.168.123.191, 192.168.123.192')
    assert _run(data) == BLOB
    assert len(calls) == 2


def test_falls_back_to_dc_ip_when_fqdn_lookup_fails(monkeypatch):
    """A DC that will not report its FQDN is still worth one attempt by IP."""
    calls = []
    monkeypatch.setattr('app.domain_odj.os.path.exists', lambda p: True)

    def fake_run(argv, **kwargs):
        if 'info' in argv:
            return _Result(1, '', 'no reply')
        calls.append(argv)
        return _Result(0, BLOB)

    monkeypatch.setattr('app.domain_odj.subprocess.run', fake_run)
    assert _run(_data()) == BLOB
    assert 'dcname=192.168.123.191' in calls[0]


def test_ensure_resolvable_adds_hosts_entry(monkeypatch, tmp_path):
    hosts = tmp_path / 'hosts'
    hosts.write_text('127.0.0.1 localhost\n')
    monkeypatch.setattr('app.domain_odj.HOSTS_FILE', str(hosts))

    seen = {'n': 0}

    def flaky_getaddrinfo(name, *a, **k):
        # Unresolvable until the hosts entry exists.
        seen['n'] += 1
        if seen['n'] == 1:
            raise socket.gaierror('name or service not known')
        return _resolves()

    monkeypatch.setattr('app.domain_odj.socket.getaddrinfo', flaky_getaddrinfo)
    assert _ensure_resolvable(DC_FQDN, '192.168.123.191') is True
    assert f'192.168.123.191 {DC_FQDN} WIN-DC01' in hosts.read_text()


def test_ensure_resolvable_is_idempotent(monkeypatch, tmp_path):
    hosts = tmp_path / 'hosts'
    hosts.write_text(f'127.0.0.1 localhost\n192.168.123.191 {DC_FQDN} WIN-DC01\n')
    monkeypatch.setattr('app.domain_odj.HOSTS_FILE', str(hosts))
    monkeypatch.setattr('app.domain_odj.socket.getaddrinfo', _unresolvable)
    assert _ensure_resolvable(DC_FQDN, '192.168.123.191') is True
    assert hosts.read_text().count(DC_FQDN) == 1


def test_ensure_resolvable_soft_fails_on_readonly_hosts(monkeypatch, tmp_path):
    monkeypatch.setattr('app.domain_odj.HOSTS_FILE', str(tmp_path / 'missing' / 'hosts'))
    monkeypatch.setattr('app.domain_odj.socket.getaddrinfo', _unresolvable)
    assert _ensure_resolvable(DC_FQDN, '192.168.123.191') is False
