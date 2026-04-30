/* QEX — client-side helpers */

// Auto-dismiss flash alerts after 6s
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.alert').forEach(el => {
    setTimeout(() => {
      const alert = bootstrap.Alert.getOrCreateInstance(el);
      if (alert) alert.close();
    }, 6000);
  });
});
