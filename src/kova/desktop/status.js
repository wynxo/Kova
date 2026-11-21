const params = new URLSearchParams(location.search);
if (params.get('state') === 'error') {
  document.querySelector('#title').textContent = 'Couldn’t start Kova';
  document.querySelector('#message').textContent = 'The local server did not start. Check the server output below, then retry.';
  document.querySelector('#pulse').hidden = true;
  document.querySelector('#error').hidden = false;
  document.querySelector('#log').textContent = params.get('log');
}
document.querySelector('#retry').onclick = () => window.kovaStartup.retry();
