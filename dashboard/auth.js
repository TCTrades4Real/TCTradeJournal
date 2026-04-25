const ALLOWED_EMAIL = 'tctrades4real@gmail.com';
const CLIENT_ID     = '413954462899-jcup1smhjuh6londv3tcsmil17cog1vv.apps.googleusercontent.com';

function initAuth(onAuthorized) {
  if (sessionStorage.getItem('auth_ok') === '1') { onAuthorized(); return; }
  document.getElementById('authOverlay').style.display = 'flex';
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

function _verify(r, onAuthorized) {
  const p = JSON.parse(atob(r.credential.split('.')[1]));
  if (p.email === ALLOWED_EMAIL) {
    sessionStorage.setItem('auth_ok', '1');
    document.getElementById('authOverlay').style.display = 'none';
    onAuthorized();
  } else {
    document.getElementById('authError').textContent = 'Access denied: ' + p.email;
  }
}
