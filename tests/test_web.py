import re


def _csrf_token(html):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert match, 'csrf_token field not found'
    return match.group(1).decode()


def test_login_page_renders_with_csrf(client):
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


def test_state_changing_post_without_csrf_is_rejected(client):
    resp = client.post('/start_clone_task', json={'template_vmid': 1})
    assert resp.status_code == 400  # CSRF rejection happens before the view


def test_protected_route_redirects_when_anonymous(client):
    resp = client.get('/')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_login_flow_and_change_password(client):
    token = _csrf_token(client.get('/login').data)
    resp = client.post('/login', data={'password': 'changeme', 'csrf_token': token})
    assert resp.status_code == 302  # successful login redirects to index

    # Now authenticated: the home page loads.
    assert client.get('/').status_code == 200

    # Change the password.
    token = _csrf_token(client.get('/change_password').data)
    resp = client.post('/change_password', data={
        'current_password': 'changeme',
        'new_password': 'a-better-password',
        'confirm_password': 'a-better-password',
        'csrf_token': token,
    })
    assert resp.status_code == 302  # success redirects to index

    # Old password no longer works after logout.
    client.get('/logout')
    token = _csrf_token(client.get('/login').data)
    resp = client.post('/login', data={'password': 'changeme', 'csrf_token': token})
    assert resp.status_code == 200  # re-rendered login page with a flash, not a redirect


def test_change_password_rejects_wrong_current(client):
    token = _csrf_token(client.get('/login').data)
    client.post('/login', data={'password': 'changeme', 'csrf_token': token})

    token = _csrf_token(client.get('/change_password').data)
    resp = client.post('/change_password', data={
        'current_password': 'wrong',
        'new_password': 'a-better-password',
        'confirm_password': 'a-better-password',
        'csrf_token': token,
    })
    assert resp.status_code == 200  # stays on the form
    assert b'Current password is incorrect' in resp.data
