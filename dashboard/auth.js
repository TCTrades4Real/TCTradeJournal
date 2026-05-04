const ALLOWED_EMAIL = 'tctrades4real@gmail.com';
const CLIENT_ID     = '413954462899-jcup1smhjuh6londv3tcsmil17cog1vv.apps.googleusercontent.com';
const REMEMBER_KEY  = 'auth_remember_exp';
const REMEMBER_DAYS = 30;

function _isRemembered() {
  const exp = localStorage.getItem(REMEMBER_KEY);
  return exp && Date.now() < parseInt(exp, 10);
}

function _injectRememberMe(card) {
  const wrap = document.createElement('label');
  wrap.id = 'rememberWrap';
  wrap.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:14px;cursor:pointer;color:#9ca3af;font-size:13px;user-select:none';
  wrap.innerHTML = '<input type="checkbox" id="rememberMe" style="width:15px;height:15px;accent-color:#3b82f6;cursor:pointer"> Remember me for 30 days';
  const errEl = card.querySelector('#authError');
  errEl ? card.insertBefore(wrap, errEl) : card.appendChild(wrap);
}

function initAuth(onAuthorized) {
  if (sessionStorage.getItem('auth_ok') === '1' || _isRemembered()) { onAuthorized(); return; }
  const overlay = document.getElementById('authOverlay');
  overlay.style.display = 'flex';
  _injectRememberMe(overlay.querySelector('.auth-card') || overlay);
  const s = document.createElement('script');
  s.src = 'https://accounts.google.com/gsi/client';
  s.onload = () => {
    google.accounts.id.initialize({
      client_id: CLIENT_ID,
      callback: r => _verify(r, onAuthorized),
      auto_select: true,
    });
    google.accounts.id.prompt();
  };
  document.head.appendChild(s);
}

async function _verify(r, onAuthorized) {
  try {
    const res  = await fetch(`https://oauth2.googleapis.com/tokeninfo?id_token=${r.credential}`);
    const data = await res.json();

    if (!res.ok || data.error) throw new Error(data.error || 'tokeninfo failed');
    if (data.aud !== CLIENT_ID)        throw new Error('token audience mismatch');
    if (data.email !== ALLOWED_EMAIL)  throw new Error('Access denied: ' + data.email);
    if (data.email_verified !== 'true') throw new Error('email not verified');

    if (document.getElementById('rememberMe')?.checked) {
      localStorage.setItem(REMEMBER_KEY, Date.now() + REMEMBER_DAYS * 864e5);
    } else {
      localStorage.removeItem(REMEMBER_KEY);
    }
    sessionStorage.setItem('auth_ok', '1');
    document.getElementById('authOverlay').style.display = 'none';
    onAuthorized();
  } catch (e) {
    document.getElementById('authError').textContent = e.message;
  }
}
