
Prerender a web page, and give timing information about how long it takes to
load.

This uses Python 3's `async` and `await` functionality to make code that is
both IO-wait heavy and also legible. The best library for asynchronous HTTP
functions now is probably `aiohttp`, in both serving and acting as a client.

I spawn a chrome/chromium process and ask it to listen on a system-assigned
port for devtools protocol access. I discover the port number in a file on
disk, and connect a websocket to it to operate on user-entered web pages.

It also listens as a web server, on a human-discoverable "/" empty path, and
offers a form to the user. There are two JSON-returning API URLs that return
rendered web page, or the resource list and download times.

When a URL is entered and submitted, we discover links to JSON resources 
of the page contents, at time of page-loaded signal, and all network resources
consumed up to page-loaded along with how long the resource took to load, in
seconds.

Next steps are

- to keep a pool of chrome processes ready, and kill off processes
  after some number of requests, and start new ones on demand.
- to keep chrome from caching information, ever, through use of site policies
  or writing configurations before starting the browser.
- to add configurability of storage location of cache.
- to handle a page never becoming Loaded.
