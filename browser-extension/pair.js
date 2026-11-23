const status = document.querySelector("#status");
const field = document.querySelector("#key");

chrome.storage.local.get("key").then(({key}) => {
  if (key) status.textContent = "Pairing code saved.";
});
document.querySelector("form").addEventListener("submit", async event => {
  event.preventDefault();
  const key = field.value.trim();
  if (!/^[a-f0-9]{32}$/.test(key)) {
    status.textContent = "Enter the 32-character pairing code.";
    return;
  }
  await chrome.storage.local.set({key});
  field.value = "";
  status.textContent = "Pairing code saved.";
});
document.querySelector("#unpair").addEventListener("click", async () => {
  await chrome.storage.local.remove("key");
  field.value = "";
  status.textContent = "Unpaired.";
});
