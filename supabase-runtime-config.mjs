const DEFAULT_SUPABASE_URL = 'https://dtzayqhebbrbvordmabh.supabase.co';
const DEFAULT_SUPABASE_BUCKET = 'spypoint-images';

const URL_ENV_NAMES = ['SUPABASE_URL', 'NEXT_PUBLIC_SUPABASE_URL'];
const KEY_ENV_NAMES = [
  'SUPABASE_SECRET_KEY',
  'SUPABASE_PUBLISHABLE_KEY',
  'TOPHAND_SUPABASE_PUBLISHABLE_KEY',
  'NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY',
  'SUPABASE_SERVICE_ROLE_KEY',
  'SUPABASE_KEY',
  'NEXT_PUBLIC_SUPABASE_ANON_KEY',
  'SUPABASE_ANON_KEY',
];
const BUCKET_ENV_NAMES = ['SUPABASE_BUCKET', 'NEXT_PUBLIC_SUPABASE_BUCKET'];

function firstEnv(names) {
  for (const name of names) {
    const value = process.env[name];
    if (value) {
      return value;
    }
  }
  return '';
}

export function getSupabaseRuntimeConfig() {
  const url = firstEnv(URL_ENV_NAMES) || DEFAULT_SUPABASE_URL;
  const key = firstEnv(KEY_ENV_NAMES);
  const bucket = firstEnv(BUCKET_ENV_NAMES) || DEFAULT_SUPABASE_BUCKET;

  if (!key) {
    throw new Error(
      `Supabase API key is not configured. Set one of ${KEY_ENV_NAMES.join(', ')} from 1Password or a protected runtime env file; do not commit it.`
    );
  }

  return { url, key, bucket };
}
