// Compact local datetime stamp (YYYYMMDD-HHMMSS) used to make exported file
// names unique and sortable. Appended to default output names across tabs.
export function timestamp(date = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}${p(date.getMonth() + 1)}${p(date.getDate())}` +
    `-${p(date.getHours())}${p(date.getMinutes())}${p(date.getSeconds())}`
  );
}
