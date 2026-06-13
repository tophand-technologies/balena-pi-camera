const SUPABASE_URL = "https://dtzayqhebbrbvordmabh.supabase.co";
const SUPABASE_BUCKET = "spypoint-images";

function jsString(value) {
  return JSON.stringify(String(value || ""));
}

module.exports = function handler(_req, res) {
  const publishableKey =
    process.env.TOPHAND_SUPABASE_PUBLISHABLE_KEY ||
    process.env.SUPABASE_PUBLISHABLE_KEY ||
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

  if (!publishableKey) {
    res.statusCode = 503;
    res.setHeader("Content-Type", "application/javascript; charset=utf-8");
    res.setHeader("Cache-Control", "no-store");
    res.end(
      [
        "window.TOPHAND_SUPABASE_CONFIG_ERROR = 'missing_publishable_key';",
        "window.TOPHAND_SUPABASE_CONFIG = window.TOPHAND_SUPABASE_CONFIG || {};",
      ].join("\n")
    );
    return;
  }

  res.statusCode = 200;
  res.setHeader("Content-Type", "application/javascript; charset=utf-8");
  res.setHeader("Cache-Control", "public, max-age=300, s-maxage=300");
  res.end(
    [
      "window.TOPHAND_SUPABASE_CONFIG = {",
      `  url: ${jsString(SUPABASE_URL)},`,
      `  publishableKey: ${jsString(publishableKey)},`,
      `  bucket: ${jsString(SUPABASE_BUCKET)},`,
      "};",
    ].join("\n")
  );
};
