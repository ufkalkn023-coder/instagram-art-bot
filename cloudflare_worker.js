export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = url.pathname.slice(1); // Remove leading slash

    // If no path is provided, return a simple 400
    if (!key) {
      return new Response("Missing object key", { status: 400 });
    }

    // Only allow GET and HEAD requests
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // Fetch the object from the R2 bucket bound to 'MY_BUCKET'
    const object = await env.MY_BUCKET.get(key);

    if (object === null) {
      return new Response("Object Not Found", { status: 404 });
    }

    const headers = new Headers();
    // Copy all object metadata headers to the response
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    
    // Add CORS headers so Instagram (and anyone else) can access it easily
    headers.set("Access-Control-Allow-Origin", "*");
    headers.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");

    // Handle HEAD request (no body)
    if (request.method === "HEAD") {
      return new Response(null, { headers });
    }

    // Handle GET request (stream the body)
    return new Response(object.body, {
      headers,
    });
  },
};
