const { Readable } = require("stream");

const MEDIA_BASE = "http://89.116.191.85";

const ALLOWED_PATHS = [
  /^\/threadgill-media\/index\.json$/,
  /^\/threadgill-media\/archive\/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json$/,
  /^\/threadgill-media\/thumbs\/[A-Za-z0-9_-]+\/[0-9]{4}\/[0-9]{2}\/[0-9]{2}\/[A-Za-z0-9_.-]+\.jpg$/,
  /^\/threadgill-media\/mms-thumbs\/[A-Za-z0-9_-]+\/[0-9]{4}\/[0-9]{2}\/[0-9]{2}\/[A-Za-z0-9_.-]+\.jpg$/,
  /^\/threadgill-media\/proxies\/[A-Za-z0-9_-]+\/[0-9]{4}\/[0-9]{2}\/[0-9]{2}\/[A-Za-z0-9_.-]+\.mp4$/,
];

module.exports = async function handler(req, res) {
  const rawPath = Array.isArray(req.query.path) ? req.query.path[0] : req.query.path;
  const path = typeof rawPath === "string" ? rawPath : "";
  if (!path || path.includes("..") || !ALLOWED_PATHS.some((pattern) => pattern.test(path))) {
    res.statusCode = 403;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "forbidden" }));
    return;
  }

  const headers = {};
  if (req.headers.range) headers.Range = req.headers.range;

  try {
    const upstream = await fetch(`${MEDIA_BASE}${path}`, { headers });
    res.statusCode = upstream.status;
    for (const header of ["content-type", "content-length", "content-range", "accept-ranges", "cache-control"]) {
      const value = upstream.headers.get(header);
      if (value) res.setHeader(header, value);
    }
    res.setHeader("X-Threadgill-Media-Bridge", "true");

    if (req.method === "HEAD" || !upstream.body) {
      res.end();
      return;
    }
    Readable.fromWeb(upstream.body).pipe(res);
  } catch (error) {
    res.statusCode = 502;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "media bridge unavailable" }));
  }
};
