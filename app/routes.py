from flask import render_template, request, Response, redirect, url_for, json, jsonify, flash
from app import app, db, celery, login_manager, csrf
from app.proxmox import (
    get_template_vms,
    get_network_bridges,
    require_windows_guest,
    require_sysprep_template,
    require_sysprep_existing_target,
    is_proxmox_template,
    use_pve_override,
    classify_windows_guest_family,
)
from app.celery_app import sysprep_workflow_task
from app.models import Task, User, BatchRequest, CustomizationSpec
from app.specs import resolve_spec_from_request, sanitize_spec_payload
from app.windows_identity import (
    WINDOWS_TIMEZONES,
    WINDOWS_LOCALES,
    DEFAULT_TIMEZONE,
    DEFAULT_LOCALE,
    DEFAULT_WORKGROUP,
)
from app.api_auth import login_or_api_token_required, with_session_csrf
from app.remotes import resolve_pve_remote
from app.launch_token import verify_launch_token
from app.provision_limits import (
    PVE_ADMIN_HINT,
    validate_resource_caps,
    check_daily_quota,
    check_storage_for_template,
    provision_limits_snapshot,
)
from app.bulk_validate import validate_bulk_items
from app.validators import ValidationError
from app.util import public_error_text
from flask_login import login_user, logout_user, login_required, current_user
import uuid
import logging


def sanitized_domain_profiles():
    """Domain profiles safe to send to the browser (credentials stripped)."""
    return {
        name: {k: v for k, v in details.items() if k not in ('domain_password', 'domain_username')}
        for name, details in app.config.get('DOMAIN_PROFILES', {}).items()
    }


def _as_request_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 't', 'yes', 'on')


def _json_field_error(message, **field_errors):
    """Additive 400 JSON: keep ``error`` string and optional ``errors`` map."""
    payload = {'error': message}
    if field_errors:
        payload['errors'] = field_errors
    return jsonify(payload), 400


def _request_submitter():
    if current_user.is_authenticated:
        return f'user:{getattr(current_user, "id", "unknown")}'
    # API tokens are not identities; keep this coarse to avoid logging secrets.
    return 'api-token'


def _task_is_running(status):
    return status in ('PENDING', 'STARTED', 'PROGRESS')


def _apply_task_filters(query):
    status = (request.args.get('status') or '').strip()
    remote_id = (request.args.get('remote_id') or '').strip()
    running_only = str(request.args.get('running') or '').lower() in ('1', 'true', 'yes')
    batch_id = (request.args.get('batch_id') or '').strip()
    if remote_id:
        query = query.filter(Task.remote_id == remote_id)
    if status:
        query = query.filter(Task.status == status)
    if running_only:
        query = query.filter(Task.status.in_(('PENDING', 'STARTED', 'PROGRESS')))
    if batch_id:
        query = query.filter(Task.batch_id == batch_id)
    kind = (request.args.get('kind') or 'customization').strip().lower()
    if kind == 'customization':
        query = query.filter(Task.name.in_(('Sysprep Workflow', 'Sysprep Existing VM', 'Clone for Sysprep')))
    elif kind != 'all':
        query = query.filter(Task.name == kind)
    return query


def _summarize_batch(batch):
    q = Task.query.filter(Task.batch_id == batch.id)
    total = q.count()
    accepted = q.filter(Task.status != 'REJECTED').count()
    rejected = q.filter(Task.status == 'REJECTED').count()
    cancelled = q.filter(Task.status == 'CANCELLED').count()
    running = q.filter(Task.status.in_(('PENDING', 'STARTED', 'PROGRESS'))).count()
    succeeded = q.filter(Task.status == 'SUCCESS').count()
    failed = q.filter(Task.status == 'FAILURE').count()
    return {
        **batch.to_dict(),
        'tasks_total': total,
        'running_items': running,
        'succeeded_items': succeeded,
        'failed_items': failed,
        'accepted_items': accepted,
        'rejected_items': rejected,
        'cancelled_items': cancelled,
    }


def _bulk_admission_ok(remote_id):
    inflight_statuses = ('PENDING', 'STARTED', 'PROGRESS')
    global_running = Task.query.filter(Task.status.in_(inflight_statuses)).count()
    if global_running >= app.config.get('BULK_MAX_CONCURRENT_GLOBAL', 10):
        return False, (
            f'Global inflight task limit reached '
            f'({app.config.get("BULK_MAX_CONCURRENT_GLOBAL", 10)}). {PVE_ADMIN_HINT}',
            {'global_limit': app.config.get('BULK_MAX_CONCURRENT_GLOBAL', 10)},
        )
    if remote_id:
        remote_running = Task.query.filter(
            Task.status.in_(inflight_statuses),
            Task.remote_id == remote_id,
        ).count()
        if remote_running >= app.config.get('BULK_MAX_CONCURRENT_PER_REMOTE', 10):
            return False, (
                f'Inflight task limit reached for remote {remote_id!r}. {PVE_ADMIN_HINT}',
                {'remote_limit': app.config.get('BULK_MAX_CONCURRENT_PER_REMOTE', 10)},
            )
    active_batches = BatchRequest.query.filter(
        BatchRequest.status.in_(('ACCEPTED', 'RUNNING'))
    ).count()
    if active_batches >= app.config.get('BULK_MAX_INFLIGHT_BATCHES', 10):
        return False, (
            f'Inflight batch limit reached. {PVE_ADMIN_HINT}',
            {'batch_limit': app.config.get('BULK_MAX_INFLIGHT_BATCHES', 10)},
        )
    return True, None


def _admit_resource_and_quota(payload, template_vmid, extra_items=1):
    """Validate family caps, daily quota, and storage.

    On success: ``(True, warnings_list, family, caps)``
    On failure: ``(False, flask_error_tuple, None, None)`` where flask_error_tuple
    is ``(response, status_code)``.
    """
    family = classify_windows_guest_family(template_vmid)
    try:
        caps = validate_resource_caps(payload, family)
        check_daily_quota(extra_items=extra_items)
        _level, storage = check_storage_for_template(template_vmid)
    except ValidationError as e:
        return False, _json_field_error(public_error_text(e)), None, None
    warnings = []
    if storage and storage.get('level') == 'warn' and storage.get('message'):
        warnings.append(storage['message'])
    return True, warnings, family, caps


def apply_domain_profile_network(data):
    """Apply DNS / VLAN from the selected domain profile (independent of join).

    Selecting a profile is a network shortcut: it fills ``dns_servers`` and
    ``vlan`` when those fields were left blank. Credentials are never applied
    here — see ``resolve_domain_join_from_request``.

    Returns ``(True, None)`` on success, or ``(False, (response, status))`` when
    a non-empty profile name does not match a configured profile.
    """
    profile_name = (data.get('domain_profile') or '').strip()
    if not profile_name:
        return True, None
    profile = app.config.get('DOMAIN_PROFILES', {}).get(profile_name)
    if not profile:
        msg = f'Unknown domain profile: {profile_name!r}'
        return False, _json_field_error(msg, domain_profile=msg)
    if not (data.get('dns_servers') or '').strip() and profile.get('dns_servers'):
        data['dns_servers'] = profile.get('dns_servers')
    if data.get('vlan') in (None, '', 'None') and profile.get('vlan') is not None:
        data['vlan'] = profile.get('vlan')
    if not (data.get('domain_ou') or '').strip() and profile.get('domain_ou'):
        data['domain_ou'] = profile.get('domain_ou')
    return True, None


def resolve_domain_join_from_request(data):
    """Resolve domain-join fields on ``data`` server-side.

    Always applies network defaults from the selected profile (DNS/VLAN) first.
    When ``join_domain`` is set and ``use_domain_profile_credentials`` is true
    (the default), credentials and domain name are taken from ``DOMAIN_PROFILES``
    — never from the request body.

    Returns ``(True, None)`` on success, or ``(False, (response, status))`` when
    the named profile is missing.
    """
    ok, err = apply_domain_profile_network(data)
    if not ok:
        return False, err

    join_domain = _as_request_bool(data.get('join_domain'), False)
    data['join_domain'] = join_domain
    if not join_domain:
        data.pop('domain_password', None)
        return True, None

    use_profile = _as_request_bool(data.get('use_domain_profile_credentials'), True)
    data['use_domain_profile_credentials'] = use_profile
    if use_profile:
        profile_name = (data.get('domain_profile') or '').strip()
        profile = app.config.get('DOMAIN_PROFILES', {}).get(profile_name)
        if not profile:
            if not profile_name:
                msg = 'Select a domain profile when using profile credentials.'
            else:
                msg = f'Unknown domain profile: {profile_name!r}'
            return False, _json_field_error(msg, domain_profile=msg)
        data['domain_name'] = profile.get('domain_name')
        data['domain_username'] = profile.get('domain_username')
        data['domain_password'] = profile.get('domain_password')
    # else: domain_name / username / password come from the request body as typed
    return True, None


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        user = User.query.get(1) # Assuming user with id 1 exists
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Invalid password')
    return render_template('login.html')


@app.route('/launch')
def launch_from_pdm():
    """One-click session from a short-lived PDM HMAC launch token.

    Query: template_vmid, remote_id, exp, jti, sig
    On success: log in the operator user and redirect to the clone+Sysprep wizard.
    """
    ok, err = verify_launch_token(
        request.args.get('exp'),
        request.args.get('template_vmid'),
        request.args.get('remote_id') or '',
        request.args.get('jti'),
        request.args.get('sig'),
    )
    if not ok:
        flash(err)
        return redirect(url_for('login'))

    user = User.query.get(1)
    if not user:
        flash('GuestOS has no operator user configured.')
        return redirect(url_for('login'))
    login_user(user)

    template_vmid = (request.args.get('template_vmid') or '').strip()
    remote_id = (request.args.get('remote_id') or '').strip()
    return redirect(url_for(
        'sysprep_form',
        template_vmid=template_vmid,
        remote_id=remote_id or None,
    ))


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        if not current_user.check_password(current_password):
            flash('Current password is incorrect.')
        elif len(new_password) < 8:
            flash('New password must be at least 8 characters long.')
        elif new_password != confirm_password:
            flash('New passwords do not match.')
        else:
            current_user.set_password(new_password)
            db.session.commit()
            flash('Password changed successfully.')
            return redirect(url_for('index'))
    return render_template('change_password.html')

@app.route('/')
@login_required
def index():
    templates = get_template_vms()
    return render_template(
        'index.html',
        templates=templates,
    )


def _reject_non_sysprep_template(template_vmid):
    """Flash + redirect home unless VMID is a Windows Proxmox template (golden image)."""
    if not template_vmid:
        flash('Missing template_vmid')
        return redirect(url_for('index'))
    try:
        require_sysprep_template(template_vmid)
    except ValueError as e:
        flash(public_error_text(e))
        return redirect(url_for('index'))
    return None

@app.route('/select', methods=['POST'])
@login_required
def select_template():
    template_vmid = request.form.get('template_vmid')
    rejected = _reject_non_sysprep_template(template_vmid)
    if rejected:
        return rejected
    return redirect(url_for('sysprep_form', template_vmid=template_vmid))

@app.route('/task_status/<task_id>')
@login_or_api_token_required
def task_status(task_id):
    task = Task.query.get(task_id)
    if task:
        return jsonify(task.to_dict())
    return jsonify({'error': 'Task not found'}), 404

@app.route('/api/health')
def api_health():
    """Liveness plus shallow dependency checks for Compose/load balancers."""
    checks = {'app': 'ok'}
    status = 'ok'
    # SQLite / DB
    try:
        db.session.execute(db.text('SELECT 1'))
        checks['database'] = 'ok'
    except Exception as e:  # noqa: BLE001
        logging.warning('health database check failed: %s', public_error_text(e, fallback='unavailable'))
        checks['database'] = 'error'
        status = 'degraded'
    # Redis (Celery broker) — skip in unit tests so CI does not require Redis.
    broker = (app.config.get('CELERY_BROKER_URL') or '').strip()
    if app.config.get('TESTING'):
        checks['redis'] = 'skipped'
    elif broker.startswith('redis'):
        try:
            import redis  # type: ignore
            client = redis.Redis.from_url(
                broker, socket_connect_timeout=1, socket_timeout=1
            )
            client.ping()
            checks['redis'] = 'ok'
        except Exception as e:  # noqa: BLE001
            logging.warning('health redis check failed: %s', public_error_text(e, fallback='unavailable'))
            checks['redis'] = 'error'
            status = 'degraded'
    else:
        checks['redis'] = 'skipped'
    code = 200 if status == 'ok' else 503
    return jsonify({
        'status': status,
        'version': app.config.get('APP_VERSION', '0.0.0'),
        'build_time': app.config.get('APP_BUILD_TIME') or None,
        'checks': checks,
    }), code


@app.route('/api/version')
def api_version():
    return jsonify({
        'version': app.config.get('APP_VERSION', '0.0.0'),
        'build_time': app.config.get('APP_BUILD_TIME') or None,
        'min_pdm_guestos': '2.3.0',
    })


@app.route('/api/tasks', methods=['GET', 'OPTIONS'])
@login_or_api_token_required
def api_list_tasks():
    """List customization jobs for PDM / operators (newest first)."""
    if request.method == 'OPTIONS':
        return ('', 204)

    try:
        limit = min(max(int(request.args.get('limit') or 100), 1), 500)
    except ValueError:
        limit = 100
    try:
        offset = max(int(request.args.get('offset') or 0), 0)
    except ValueError:
        offset = 0

    q = _apply_task_filters(Task.query)
    total = q.count()
    cursor = (request.args.get('cursor') or '').strip()
    ordered = q.order_by(Task.timestamp.desc(), Task.id.desc())
    if cursor:
        try:
            rows = ordered.filter(Task.id < cursor).limit(limit).all()
        except Exception:
            rows = ordered.offset(offset).limit(limit).all()
    else:
        rows = ordered.offset(offset).limit(limit).all()
    next_cursor = rows[-1].id if rows else None
    return jsonify({
        'tasks': [t.to_dict() for t in rows],
        'count': len(rows),
        'total': total,
        'limit': limit,
        'offset': offset,
        'next_cursor': next_cursor,
    })


@app.route('/api/tasks/<task_id>', methods=['GET', 'OPTIONS'])
@login_or_api_token_required
def api_get_task(task_id):
    if request.method == 'OPTIONS':
        return ('', 204)
    task = Task.query.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task.to_dict())


@app.route('/jobs')
@login_required
def jobs_page():
    """Simple GuestOS job history page (same ledger as the PDM tab)."""
    batch_id = (request.args.get('batch_id') or '').strip()
    q = Task.query
    if batch_id:
        q = q.filter(Task.batch_id == batch_id)
    rows = q.order_by(Task.timestamp.desc()).limit(500).all()
    batch = BatchRequest.query.get(batch_id) if batch_id else None
    return render_template('jobs.html', tasks=rows, batch_id=batch_id or None, batch=batch)


@app.route('/api/metrics')
@login_or_api_token_required
def api_metrics():
    inflight_statuses = ('PENDING', 'STARTED', 'PROGRESS')
    by_status = {
        s: Task.query.filter(Task.status == s).count()
        for s in ('PENDING', 'STARTED', 'PROGRESS', 'SUCCESS', 'FAILURE', 'CANCELLED')
    }
    by_remote = {}
    remote_rows = (
        db.session.query(Task.remote_id, db.func.count(Task.id))
        .filter(Task.status.in_(inflight_statuses))
        .group_by(Task.remote_id)
        .all()
    )
    for remote_id, count in remote_rows:
        by_remote[remote_id or 'default'] = int(count)
    return jsonify({
        'app_version': app.config.get('APP_VERSION', '0.0.0'),
        'build_time': app.config.get('APP_BUILD_TIME') or None,
        'tasks': {
            'inflight': sum(by_status.get(s, 0) for s in inflight_statuses),
            'by_status': by_status,
            'inflight_by_remote': by_remote,
        },
        'batches': {
            'running': BatchRequest.query.filter(BatchRequest.status == 'RUNNING').count(),
            'accepted': BatchRequest.query.filter(BatchRequest.status == 'ACCEPTED').count(),
            'cancelled': BatchRequest.query.filter(BatchRequest.status == 'CANCELLED').count(),
            'failed': BatchRequest.query.filter(BatchRequest.status == 'FAILED').count(),
        },
    })


@app.route('/api/provision_limits', methods=['GET', 'OPTIONS'])
@login_or_api_token_required
def api_provision_limits():
    """Return resource ceilings, remaining daily/batch quota, and storage status."""
    if request.method == 'OPTIONS':
        return ('', 204)
    template_vmid = (request.args.get('template_vmid') or '').strip() or None
    remote_id = (request.args.get('remote_id') or '').strip() or None
    family = 'win11'
    from app.remotes import attach_pve_override
    data = {}
    if template_vmid:
        data['template_vmid'] = template_vmid
    if remote_id:
        data['remote_id'] = remote_id
    try:
        if data:
            ok, err = resolve_pve_remote(data, _json_field_error)
            if not ok:
                snap = provision_limits_snapshot(family='win11', template_vmid=None)
                snap['warning'] = 'Could not resolve remote; showing default Win11 caps only.'
                return jsonify(snap)
            try:
                pve_override = attach_pve_override(data)
            except ValueError:
                pve_override = None
            with use_pve_override(pve_override):
                if template_vmid:
                    family = classify_windows_guest_family(template_vmid)
                return jsonify(
                    provision_limits_snapshot(family=family, template_vmid=template_vmid)
                )
    except Exception as e:
        logging.warning('provision_limits failed (%s)', type(e).__name__)
        snap = provision_limits_snapshot(family='win11', template_vmid=None)
        snap['warning'] = 'Could not query provisioning limits; showing defaults.'
        return jsonify(snap)
    return jsonify(provision_limits_snapshot(family='win11', template_vmid=None))


@app.route('/workflow/<task_id>')
@login_required
def workflow(task_id):
    return render_template('workflow.html', task_id=task_id)

@app.route('/sysprep_form', methods=['GET', 'POST'])
@login_required
def sysprep_form():
    """Golden-image path: Windows template → clone → Sysprep (VMware-style)."""
    if request.method == 'GET':
        template_vmid = request.args.get('template_vmid')
    else:
        template_vmid = request.form.get('template_vmid')
    rejected = _reject_non_sysprep_template(template_vmid)
    if rejected:
        return rejected
    remote_id = (request.values.get('remote_id') or request.args.get('remote_id') or '').strip()
    bridges = get_network_bridges()
    specs = CustomizationSpec.query.order_by(CustomizationSpec.name.asc()).all()
    return render_template(
        'sysprep_form.html',
        template_vmid=template_vmid,
        bridges=bridges,
        domain_profiles=sanitized_domain_profiles(),
        remote_id=remote_id,
        timezones=WINDOWS_TIMEZONES,
        locales=WINDOWS_LOCALES,
        default_timezone=DEFAULT_TIMEZONE,
        default_locale=DEFAULT_LOCALE,
        default_workgroup=DEFAULT_WORKGROUP,
        specs=specs,
    )

@app.route('/start_sysprep_workflow', methods=['POST'])
@csrf.exempt
@login_or_api_token_required
@with_session_csrf
def start_sysprep_workflow():
    data = request.json or {}
    ok, err = resolve_spec_from_request(data)
    if not ok:
        msg, fields = err
        return _json_field_error(msg, **fields)
    ok, err = resolve_domain_join_from_request(data)
    if not ok:
        return err
    ok, err = resolve_pve_remote(data, _json_field_error)
    if not ok:
        return err

    template_vmid = data.get('template_vmid')
    if not template_vmid:
        return _json_field_error('template_vmid is required.', template_vmid='Required.')

    # Validate template with resolved remotes, but never enqueue PVE secrets.
    from app.remotes import attach_pve_override
    try:
        pve_override = attach_pve_override(data)
    except ValueError as e:
        msg = public_error_text(e, fallback='Invalid remote.')
        return _json_field_error(msg, remote_id=msg)
    with use_pve_override(pve_override):
        try:
            require_sysprep_template(template_vmid)
        except ValueError as e:
            return jsonify({'error': public_error_text(e, fallback='Invalid template.')}), 400
        ok_admit, admit_result, _family, _caps = _admit_resource_and_quota(
            data, template_vmid, extra_items=1
        )
        if not ok_admit:
            return admit_result
        warnings = admit_result
        family = classify_windows_guest_family(template_vmid)
        if _as_request_bool(data.get('manage_disks')) and family != 'server':
            return _json_field_error(
                'manage_disks / Configure disks is only available for Windows Server '
                'templates. Windows 11 keeps a flat disk layout.',
                manage_disks='Not allowed for Win11.',
            )

    # API / PDM clients may omit bridge; default to the configured primary bridge.
    if not (data.get('bridge') or '').strip():
        data['bridge'] = app.config.get('PRIMARY_BRIDGE') or 'vmbr0'

    # Celery payload must not carry server-side PVE credentials.
    data.pop('_pve', None)

    task_id = str(uuid.uuid4())
    vm_uuid = str(uuid.uuid4())
    hostname = data.get('hostname')

    task = Task(
        id=task_id,
        name='Sysprep Workflow',
        description=f'Starting Sysprep workflow for {hostname}',
        vm_uuid=vm_uuid,
        hostname=(hostname or None),
        template_vmid=int(data['template_vmid']) if str(data.get('template_vmid', '')).isdigit() else None,
        remote_id=(data.get('remote_id') or None),
        submitter=_request_submitter(),
    )
    db.session.add(task)
    db.session.commit()

    sysprep_workflow_task.apply_async(
        args=(task_id, data),
        queue='clone_queue',
    )

    resp = {'task_id': task_id}
    if warnings:
        resp['warnings'] = warnings
    return jsonify(resp)


@app.route('/start_sysprep_bulk_workflow', methods=['POST'])
@csrf.exempt
@login_or_api_token_required
@with_session_csrf
def start_sysprep_bulk_workflow():
    payload = request.json or {}
    shared = dict(payload.get('shared') or {})
    items = payload.get('items') or []
    if not isinstance(items, list) or not items:
        return _json_field_error('items is required and must be a non-empty array.', items='Required.')
    # Apply Customization Spec to shared defaults (request values still win).
    ok, err = resolve_spec_from_request(shared)
    if not ok:
        msg, fields = err
        return _json_field_error(msg, **fields)
    # Bulk is Win11 single-NIC only — reject multi-NIC layouts.
    if isinstance(shared.get('nics'), list) and len(shared.get('nics')) > 1:
        return _json_field_error(
            'Bulk provisioning supports a single NIC only (Windows 11).',
            nics='Multi-NIC is not available for bulk.',
        )
    max_items = app.config.get('BULK_MAX_ITEMS', 10)
    if len(items) > max_items:
        return _json_field_error(
            f'Batch exceeds max items ({max_items}). {PVE_ADMIN_HINT}',
            items=f'Max {max_items}.',
        )

    network_mode = (shared.get('network_mode') or 'static').strip().lower()
    try:
        validate_bulk_items(items, network_mode=network_mode)
    except ValidationError as e:
        msg = public_error_text(e, fallback='Invalid bulk items.')
        return _json_field_error(msg, items=msg)

    request_id = (
        request.headers.get('Idempotency-Key')
        or payload.get('request_id')
        or str(uuid.uuid4())
    ).strip()
    if not request_id:
        request_id = str(uuid.uuid4())

    existing = BatchRequest.query.filter_by(request_id=request_id).first()
    if existing:
        existing.idempotent_replay = True
        existing.updated_at = db.func.now()
        db.session.commit()
        return jsonify({
            'batch_id': existing.id,
            'request_id': existing.request_id,
            'idempotent_replay': True,
            'summary': _summarize_batch(existing),
        }), 200

    template_vmid = shared.get('template_vmid')
    remote_id = (shared.get('remote_id') or '').strip() or None
    ok, details = _bulk_admission_ok(remote_id)
    if not ok:
        return _json_field_error(details[0], **details[1])

    # Shared resource/quota/storage admit (uses shared payload + template).
    from app.remotes import attach_pve_override
    try:
        pve_override_shared = attach_pve_override(dict(shared))
    except ValueError as e:
        msg = public_error_text(e, fallback='Invalid remote.')
        return _json_field_error(msg, remote_id=msg)
    warnings = []
    with use_pve_override(pve_override_shared):
        if template_vmid:
            try:
                require_sysprep_template(template_vmid)
            except ValueError as e:
                return jsonify({'error': public_error_text(e, fallback='Invalid template.')}), 400
            family = classify_windows_guest_family(template_vmid)
            if family != 'win11':
                return _json_field_error(
                    'Bulk/batch provisioning is only available for Windows 11 '
                    'templates (tag windows11). Windows Server templates must use '
                    'single Customize. ' + PVE_ADMIN_HINT,
                    template_vmid='Bulk not allowed for Server templates.',
                )
            if _as_request_bool(shared.get('manage_disks')):
                return _json_field_error(
                    'manage_disks / Configure disks is not available for Windows 11 '
                    'bulk provisioning (flat disk layout).',
                    manage_disks='Not allowed for Win11.',
                )
            ok_admit, admit_result, _family, _caps = _admit_resource_and_quota(
                shared, template_vmid, extra_items=len(items)
            )
            if not ok_admit:
                return admit_result
            warnings = admit_result

    batch_id = str(uuid.uuid4())
    batch = BatchRequest(
        id=batch_id,
        request_id=request_id,
        status='RUNNING',
        submitted_by=_request_submitter(),
        remote_id=remote_id,
        template_vmid=int(template_vmid) if str(template_vmid or '').isdigit() else None,
        total_items=len(items),
        message='Batch accepted.',
    )
    db.session.add(batch)
    db.session.flush()

    accepted = 0
    rejected = 0
    task_refs = []

    for idx, item in enumerate(items, start=1):
        data = dict(shared)
        data.update(item or {})
        if not (data.get('hostname') or '').strip():
            rejected += 1
            continue
        ok, err = resolve_domain_join_from_request(data)
        if not ok:
            rejected += 1
            continue
        ok, err = resolve_pve_remote(data, _json_field_error)
        if not ok:
            rejected += 1
            continue
        tvmid = data.get('template_vmid')
        if not tvmid:
            rejected += 1
            continue
        try:
            pve_override = attach_pve_override(data)
        except ValueError:
            rejected += 1
            continue
        with use_pve_override(pve_override):
            try:
                require_sysprep_template(tvmid)
            except ValueError:
                rejected += 1
                continue
        if not (data.get('bridge') or '').strip():
            data['bridge'] = app.config.get('PRIMARY_BRIDGE') or 'vmbr0'
        data.pop('_pve', None)

        task_id = str(uuid.uuid4())
        vm_uuid = str(uuid.uuid4())
        hostname = data.get('hostname')
        task = Task(
            id=task_id,
            name='Sysprep Workflow',
            description=f'Batch {batch_id} item {idx}: {hostname}',
            vm_uuid=vm_uuid,
            hostname=(hostname or None),
            template_vmid=int(data['template_vmid']) if str(data.get('template_vmid', '')).isdigit() else None,
            remote_id=(data.get('remote_id') or None),
            batch_id=batch_id,
            request_id=request_id,
            sequence_no=idx,
            submitter=batch.submitted_by,
        )
        db.session.add(task)
        accepted += 1
        task_refs.append((task_id, data))

    batch.accepted_items = accepted
    batch.rejected_items = rejected
    batch.status = 'RUNNING' if accepted else 'FAILED'
    if not accepted:
        batch.message = 'No valid items were accepted from this batch.'
    db.session.commit()

    for task_id, data in task_refs:
        sysprep_workflow_task.apply_async(
            args=(task_id, data),
            queue='clone_queue',
        )

    resp = {
        'batch_id': batch_id,
        'request_id': request_id,
        'accepted_count': accepted,
        'rejected_count': rejected,
        'task_ids': [t for t, _ in task_refs],
        'idempotent_replay': False,
    }
    if warnings:
        resp['warnings'] = warnings
    return jsonify(resp), 200


@app.route('/api/batches', methods=['GET', 'OPTIONS'])
@login_or_api_token_required
def api_list_batches():
    if request.method == 'OPTIONS':
        return ('', 204)
    try:
        limit = min(max(int(request.args.get('limit') or 100), 1), 500)
    except ValueError:
        limit = 100
    try:
        offset = max(int(request.args.get('offset') or 0), 0)
    except ValueError:
        offset = 0
    status = (request.args.get('status') or '').strip()
    remote_id = (request.args.get('remote_id') or '').strip()
    q = BatchRequest.query
    if status:
        q = q.filter(BatchRequest.status == status)
    if remote_id:
        q = q.filter(BatchRequest.remote_id == remote_id)
    total = q.count()
    rows = q.order_by(BatchRequest.timestamp.desc()).offset(offset).limit(limit).all()
    return jsonify({
        'batches': [_summarize_batch(b) for b in rows],
        'count': len(rows),
        'total': total,
        'limit': limit,
        'offset': offset,
    })


@app.route('/api/batches/<batch_id>', methods=['GET', 'OPTIONS'])
@login_or_api_token_required
def api_get_batch(batch_id):
    if request.method == 'OPTIONS':
        return ('', 204)
    batch = BatchRequest.query.get(batch_id)
    if not batch:
        return jsonify({'error': 'Batch not found'}), 404
    tasks = (
        Task.query.filter(Task.batch_id == batch_id)
        .order_by(Task.sequence_no.asc(), Task.timestamp.asc())
        .limit(1000)
        .all()
    )
    return jsonify({
        'batch': _summarize_batch(batch),
        'tasks': [t.to_dict() for t in tasks],
    })


@app.route('/api/batches/<batch_id>/cancel', methods=['POST', 'OPTIONS'])
@csrf.exempt
@login_or_api_token_required
@with_session_csrf
def api_cancel_batch(batch_id):
    if request.method == 'OPTIONS':
        return ('', 204)
    batch = BatchRequest.query.get(batch_id)
    if not batch:
        return jsonify({'error': 'Batch not found'}), 404
    cancellable = (
        Task.query.filter(
            Task.batch_id == batch_id,
            Task.status.in_(('PENDING', 'STARTED', 'PROGRESS')),
        )
        .all()
    )
    for task in cancellable:
        task.status = 'CANCELLED'
        task.progress = 100
        task.message = 'Cancelled by operator.'
    batch.status = 'CANCELLED'
    batch.cancelled_items = len(cancellable)
    batch.message = 'Batch cancelled; running tasks marked cancelled best-effort.'
    db.session.commit()
    return jsonify({
        'batch_id': batch_id,
        'cancelled_items': len(cancellable),
    }), 200

@app.route('/specs')
@login_required
def specs_page():
    specs = CustomizationSpec.query.order_by(CustomizationSpec.name.asc()).all()
    return render_template('specs.html', specs=specs)


@app.route('/specs/new', methods=['GET', 'POST'])
@login_required
def specs_new():
    if request.method == 'GET':
        return render_template(
            'spec_form.html',
            spec=None,
            timezones=WINDOWS_TIMEZONES,
            locales=WINDOWS_LOCALES,
            default_timezone=DEFAULT_TIMEZONE,
            default_locale=DEFAULT_LOCALE,
            default_workgroup=DEFAULT_WORKGROUP,
            domain_profiles=sanitized_domain_profiles(),
        )
    name = (request.form.get('name') or '').strip()
    description = (request.form.get('description') or '').strip()
    if not name:
        flash('Name is required.')
        return redirect(url_for('specs_new'))
    if CustomizationSpec.query.filter_by(name=name).first():
        flash(f'A spec named "{name}" already exists.')
        return redirect(url_for('specs_new'))
    spec = CustomizationSpec(name=name, description=description)
    spec.set_payload(_spec_payload_from_form(request.form))
    db.session.add(spec)
    db.session.commit()
    flash(f'Spec "{name}" created.')
    return redirect(url_for('specs_page'))


@app.route('/specs/<int:spec_id>/edit', methods=['GET', 'POST'])
@login_required
def specs_edit(spec_id):
    spec = CustomizationSpec.query.get_or_404(spec_id)
    if request.method == 'GET':
        return render_template(
            'spec_form.html',
            spec=spec,
            timezones=WINDOWS_TIMEZONES,
            locales=WINDOWS_LOCALES,
            default_timezone=DEFAULT_TIMEZONE,
            default_locale=DEFAULT_LOCALE,
            default_workgroup=DEFAULT_WORKGROUP,
            domain_profiles=sanitized_domain_profiles(),
        )
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Name is required.')
        return redirect(url_for('specs_edit', spec_id=spec.id))
    clash = CustomizationSpec.query.filter(
        CustomizationSpec.name == name,
        CustomizationSpec.id != spec.id,
    ).first()
    if clash:
        flash(f'A spec named "{name}" already exists.')
        return redirect(url_for('specs_edit', spec_id=spec.id))
    spec.name = name
    spec.description = (request.form.get('description') or '').strip()
    spec.set_payload(_spec_payload_from_form(request.form))
    db.session.commit()
    flash(f'Spec "{name}" updated.')
    return redirect(url_for('specs_page'))


@app.route('/specs/<int:spec_id>/delete', methods=['POST'])
@login_required
def specs_delete(spec_id):
    spec = CustomizationSpec.query.get_or_404(spec_id)
    name = spec.name
    db.session.delete(spec)
    db.session.commit()
    flash(f'Spec "{name}" deleted.')
    return redirect(url_for('specs_page'))


@app.route('/api/specs', methods=['GET', 'POST', 'OPTIONS'])
@csrf.exempt
@login_or_api_token_required
@with_session_csrf
def api_specs():
    if request.method == 'OPTIONS':
        return '', 204
    if request.method == 'GET':
        specs = CustomizationSpec.query.order_by(CustomizationSpec.name.asc()).all()
        return jsonify({'specs': [s.to_dict() for s in specs]})
    body = request.json or {}
    name = (body.get('name') or '').strip()
    if not name:
        return _json_field_error('name is required.', name='Required.')
    if CustomizationSpec.query.filter_by(name=name).first():
        return _json_field_error(f'Spec "{name}" already exists.', name='Duplicate.')
    spec = CustomizationSpec(name=name, description=(body.get('description') or '').strip())
    spec.set_payload(sanitize_spec_payload(body.get('payload') or body))
    db.session.add(spec)
    db.session.commit()
    return jsonify(spec.to_dict()), 201


@app.route('/api/specs/<int:spec_id>', methods=['GET', 'PUT', 'DELETE', 'OPTIONS'])
@csrf.exempt
@login_or_api_token_required
@with_session_csrf
def api_spec_detail(spec_id):
    if request.method == 'OPTIONS':
        return '', 204
    spec = CustomizationSpec.query.get(spec_id)
    if not spec:
        return jsonify({'error': 'Not found.'}), 404
    if request.method == 'GET':
        return jsonify(spec.to_dict())
    if request.method == 'DELETE':
        db.session.delete(spec)
        db.session.commit()
        return jsonify({'ok': True})
    body = request.json or {}
    if 'name' in body:
        name = (body.get('name') or '').strip()
        if not name:
            return _json_field_error('name is required.', name='Required.')
        clash = CustomizationSpec.query.filter(
            CustomizationSpec.name == name,
            CustomizationSpec.id != spec.id,
        ).first()
        if clash:
            return _json_field_error(f'Spec "{name}" already exists.', name='Duplicate.')
        spec.name = name
    if 'description' in body:
        spec.description = (body.get('description') or '').strip()
    if 'payload' in body:
        spec.set_payload(sanitize_spec_payload(body.get('payload')))
    db.session.commit()
    return jsonify(spec.to_dict())


def _spec_payload_from_form(form):
    """Build a non-secret spec payload from the Specs HTML form."""
    payload = {
        'timezone': form.get('timezone') or DEFAULT_TIMEZONE,
        'locale': form.get('locale') or DEFAULT_LOCALE,
        'workgroup': form.get('workgroup') or DEFAULT_WORKGROUP,
        'network_mode': form.get('network_mode') or 'static',
        'bridge': form.get('bridge') or '',
        'vlan': form.get('vlan') or '',
        'ip_address': form.get('ip_address') or '',
        'netmask_cidr': form.get('netmask_cidr') or '',
        'gateway': form.get('gateway') or '',
        'dns_servers': form.get('dns_servers') or '',
        'enable_ipv6': form.get('enable_ipv6') in ('on', 'true', '1'),
        'ipv6_address': form.get('ipv6_address') or '',
        'ipv6_prefix': form.get('ipv6_prefix') or '',
        'ipv6_gateway': form.get('ipv6_gateway') or '',
        'join_domain': form.get('join_domain') in ('on', 'true', '1'),
        'domain_profile': form.get('domain_profile') or '',
        'domain_ou': form.get('domain_ou') or '',
        'domain_name': form.get('domain_name') or '',
    }
    cores = form.get('cores')
    ram = form.get('ram')
    if cores:
        try:
            payload['cores'] = int(cores)
        except ValueError:
            pass
    if ram:
        try:
            payload['ram'] = int(ram)
        except ValueError:
            pass
    return sanitize_spec_payload(payload)


@app.route('/clone_form')
@login_required
def clone_form():
    """Deep-link: old PDM/bookmarks land on the Clone + Sysprep wizard."""
    template_vmid = request.args.get('template_vmid')
    remote_id = (request.args.get('remote_id') or '').strip()
    return redirect(url_for(
        'sysprep_form',
        template_vmid=template_vmid,
        remote_id=remote_id or None,
    ))


@app.route('/sysprep_existing_vm_form/<vmid>')
@login_required
def sysprep_existing_vm_form(vmid):
    """Disabled: in-place Sysprep risks production VMs. Templates → clone+Sysprep."""
    remote_id = (request.args.get('remote_id') or '').strip()
    if is_proxmox_template(vmid):
        flash(
            f'VMID {vmid} is a template. Starting Clone + Sysprep '
            '(golden image → clone → customize).'
        )
        return redirect(url_for(
            'sysprep_form',
            template_vmid=vmid,
            remote_id=remote_id or None,
        ))
    flash(
        'In-place Sysprep of existing VMs is disabled to protect production guests. '
        'Use a Windows template with Clone + Sysprep.'
    )
    return redirect(url_for('index'))

@app.route('/start_sysprep_existing_vm_task', methods=['POST'])
@csrf.exempt
@login_or_api_token_required
@with_session_csrf
def start_sysprep_existing_vm_task():
    """Always reject: in-place Sysprep of existing/production VMs is not allowed."""
    data = request.json or {}
    vmid = data.get('vmid')
    try:
        require_sysprep_existing_target(vmid or '?')
    except ValueError as e:
        return jsonify({'error': public_error_text(e, fallback='In-place Sysprep is disabled.')}), 403
    return jsonify({'error': 'In-place Sysprep is disabled.'}), 403