const baseInput = document.getElementById('base');
const DEFAULT_BASE = 'http://localhost:8000';

chrome.storage.local.get(['base'], (r) => {
  baseInput.value = r.base || DEFAULT_BASE;
});

document.getElementById('go').addEventListener('click', async () => {
  const base = (baseInput.value || DEFAULT_BASE).replace(/\/+$/, '');
  chrome.storage.local.set({ base });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab && tab.url ? tab.url : '';
  chrome.tabs.create({ url: `${base}/monitors/new?url=${encodeURIComponent(url)}` });
  window.close();
});
