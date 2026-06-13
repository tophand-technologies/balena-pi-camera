(function () {
  const DEFAULT_SUPABASE_URL = 'https://dtzayqhebbrbvordmabh.supabase.co';
  const DEFAULT_SUPABASE_BUCKET = 'spypoint-images';
  const LOCAL_STORAGE_KEYS = [
    'TOPHAND_SUPABASE_PUBLISHABLE_KEY',
    'NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY',
    'NEXT_PUBLIC_SUPABASE_ANON_KEY',
  ];

  function readLocalStorage(names) {
    try {
      for (const name of names) {
        const value = window.localStorage.getItem(name);
        if (value) {
          return value;
        }
      }
    } catch (_error) {
      return '';
    }
    return '';
  }

  window.getTophandSupabaseConfig = function getTophandSupabaseConfig() {
    const config = window.TOPHAND_SUPABASE_CONFIG || {};
    const url = config.url || DEFAULT_SUPABASE_URL;
    const bucket = config.bucket || DEFAULT_SUPABASE_BUCKET;
    const key = config.publishableKey || config.anonKey || readLocalStorage(LOCAL_STORAGE_KEYS);

    if (!key) {
      throw new Error(
        'Supabase browser key is not configured. Provide window.TOPHAND_SUPABASE_CONFIG.publishableKey before loading the viewer, or set TOPHAND_SUPABASE_PUBLISHABLE_KEY in localStorage. Do not commit keys.'
      );
    }

    return { url, key, bucket };
  };
})();
