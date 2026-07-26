from flask import render_template, request, Response, redirect, url_for, json, jsonify, flash
from app import app, db, celery, login_manager, csrf
from app.proxmox import get_template_vms, get_network_bridges, require_windows_guest, require_sysprep_template, require_sysprep_existing_target, is_proxmox_template, use_pve_override
from app.celery_app import sysprep_workflow_task
from app.models import Task, User
from app.api_auth import login_or_api_token_required, with_session_csrf
from app.remotes import resolve_pve_remote
from app.launch_token import verify_launch_token
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
        flash(str(e))
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
    return jsonify({'status': 'ok'})

@app.route('/api/version')
def api_version():
    return jsonify({'version': app.config.get('APP_VERSION', '0.0.0')})


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
    status = (request.args.get('status') or '').strip()
    remote_id = (request.args.get('remote_id') or '').strip()
    running_only = str(request.args.get('running') or '').lower() in ('1', 'true', 'yes')

    q = Task.query
    if remote_id:
        q = q.filter(Task.remote_id == remote_id)
    if status:
        q = q.filter(Task.status == status)
    if running_only:
        q = q.filter(Task.status.in_(('PENDING', 'STARTED', 'PROGRESS')))
    # Prefer customization-related jobs; still include others when not filtered.
    kind = (request.args.get('kind') or 'customization').strip().lower()
    if kind == 'customization':
        q = q.filter(Task.name.in_(('Sysprep Workflow', 'Sysprep Existing VM', 'Clone for Sysprep')))
    elif kind == 'all':
        pass
    else:
        q = q.filter(Task.name == kind)

    rows = q.order_by(Task.timestamp.desc()).limit(limit).all()
    return jsonify({
        'tasks': [t.to_dict() for t in rows],
        'count': len(rows),
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
    rows = (
        Task.query
        .order_by(Task.timestamp.desc())
        .limit(200)
        .all()
    )
    return render_template('jobs.html', tasks=rows)


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
    return render_template(
        'sysprep_form.html',
        template_vmid=template_vmid,
        bridges=bridges,
        domain_profiles=sanitized_domain_profiles(),
        remote_id=remote_id,
    )

@app.route('/start_sysprep_workflow', methods=['POST'])
@csrf.exempt
@login_or_api_token_required
@with_session_csrf
def start_sysprep_workflow():
    data = request.json or {}
    ok, err = resolve_domain_join_from_request(data)
    if not ok:
        return err
    ok, err = resolve_pve_remote(data, _json_field_error)
    if not ok:
        return err

    template_vmid = data.get('template_vmid')
    if not template_vmid:
        return _json_field_error('template_vmid is required.', template_vmid='Required.')
    with use_pve_override(data.get('_pve')):
        try:
            require_sysprep_template(template_vmid)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    # API / PDM clients may omit bridge; default to the configured primary bridge.
    if not (data.get('bridge') or '').strip():
        data['bridge'] = app.config.get('PRIMARY_BRIDGE') or 'vmbr0'

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
    )
    db.session.add(task)
    db.session.commit()

    sysprep_workflow_task.delay(task_id, data)

    return jsonify({'task_id': task_id})

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
        return jsonify({'error': str(e)}), 403
    return jsonify({'error': 'In-place Sysprep is disabled.'}), 403