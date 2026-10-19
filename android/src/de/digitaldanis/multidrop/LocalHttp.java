package de.digitaldanis.multidrop;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import javax.net.ServerSocketFactory;

/** Small bounded, one-request-per-connection HTTP server for LAN transfers. */
public final class LocalHttp implements AutoCloseable {
  public interface Handler {
    void handle(Request request, Response response) throws Exception;
  }

  private final ServerSocket server;
  private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
  private final ThreadPoolExecutor workers =
      new ThreadPoolExecutor(0, 8, 30, TimeUnit.SECONDS, new SynchronousQueue<>());
  private volatile boolean closed;

  public LocalHttp(ServerSocketFactory factory, int port, Handler handler) throws IOException {
    server = factory.createServerSocket();
    server.setReuseAddress(true);
    server.bind(new InetSocketAddress(port));
    Thread accept =
        new Thread(
            () -> {
              while (!closed)
                try {
                  Socket socket = server.accept();
                  socket.setSoTimeout(30000);
                  sockets.add(socket);
                  try {
                    workers.execute(() -> serve(socket, handler));
                  } catch (RejectedExecutionException e) {
                    sockets.remove(socket);
                    socket.close();
                  }
                } catch (IOException e) {
                  if (!closed) close();
                }
            },
            "multidrop-http");
    accept.setDaemon(true);
    accept.start();
  }

  public int port() {
    return server.getLocalPort();
  }

  private void serve(Socket socket, Handler handler) {
    try (Socket ignored = socket) {
      Response response = new Response(socket.getOutputStream());
      try {
        handler(new Request(socket), response, handler);
      } catch (BadRequest e) {
        if (!response.sent) response.text(400, "Invalid request");
      } catch (Exception e) {
        if (!response.sent) response.text(500, "Transfer failed");
      }
    } catch (IOException ignored) {
    } finally {
      sockets.remove(socket);
    }
  }

  private static void handler(Request r, Response s, Handler handler) throws Exception {
    handler.handle(r, s);
    if (!s.sent) s.text(404, "Not found");
  }

  public static final class BadRequest extends IOException {
    public BadRequest(String message) {
      super(message);
    }
  }

  public static final class Request {
    public final String method, path, remote;
    public final Map<String, String> query = new HashMap<>(), headers = new HashMap<>();
    public final InputStream body;

    Request(Socket socket) throws IOException {
      remote = socket.getInetAddress().getHostAddress();
      InputStream in = new BufferedInputStream(socket.getInputStream());
      String[] start = line(in).split(" ");
      if (start.length != 3 || !start[2].equals("HTTP/1.1") || !start[1].startsWith("/"))
        throw new BadRequest("Request line");
      method = start[0];
      String[] target = start[1].split("\\?", 2);
      path = target[0];
      if (target.length == 2)
        for (String pair : target[1].split("&")) {
          String[] v = pair.split("=", 2);
          query.put(decode(v[0]), v.length == 2 ? decode(v[1]) : "");
        }
      int total = 0;
      String h;
      while (!(h = line(in)).isEmpty()) {
        total += h.length();
        if (total > 32768 || headers.size() >= 64) throw new BadRequest("Headers too large");
        int colon = h.indexOf(':');
        if (colon <= 0) throw new BadRequest("Header");
        String key = h.substring(0, colon).toLowerCase(Locale.ROOT);
        if (headers.containsKey(key)) throw new BadRequest("Duplicate header");
        headers.put(key, h.substring(colon + 1).trim());
      }
      String encoding = headers.get("transfer-encoding");
      if (encoding != null) {
        if (!encoding.equalsIgnoreCase("chunked") || headers.containsKey("content-length"))
          throw new BadRequest("Ambiguous body");
        body = new Chunked(in);
      } else {
        long length;
        try {
          length = Long.parseLong(headers.getOrDefault("content-length", "0"));
        } catch (NumberFormatException e) {
          throw new BadRequest("Length");
        }
        if (length < 0) throw new BadRequest("Length");
        body = new Limited(in, length);
      }
      String expect = headers.get("expect");
      if (expect != null) {
        if (!expect.equalsIgnoreCase("100-continue")) throw new BadRequest("Expectation");
        socket
            .getOutputStream()
            .write("HTTP/1.1 100 Continue\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
      }
    }

    public String json() throws IOException {
      ByteArrayOutputStream b = new ByteArrayOutputStream();
      byte[] chunk = new byte[8192];
      int n;
      while ((n = body.read(chunk)) != -1) {
        if (b.size() + n > 1048576) throw new BadRequest("JSON too large");
        b.write(chunk, 0, n);
      }
      return b.toString("UTF-8");
    }
  }

  public static final class Response {
    private final OutputStream out;
    private boolean sent;

    Response(OutputStream out) {
      this.out = out;
    }

    public void text(int code, String text) throws IOException {
      bytes(code, "text/plain; charset=utf-8", text.getBytes(StandardCharsets.UTF_8));
    }

    public void json(int code, String json) throws IOException {
      bytes(code, "application/json", json.getBytes(StandardCharsets.UTF_8));
    }

    public void bytes(int code, String type, byte[] bytes) throws IOException {
      begin(code, type, bytes.length, Collections.emptyMap());
      out.write(bytes);
      out.flush();
    }

    public OutputStream begin(int code, String type, long length, Map<String, String> extra)
        throws IOException {
      if (sent) throw new IOException("Response already sent");
      sent = true;
      StringBuilder h =
          new StringBuilder(
              "HTTP/1.1 "
                  + code
                  + " "
                  + (code == 200 ? "OK" : "Response")
                  + "\r\nConnection: close\r\nContent-Length: "
                  + length
                  + "\r\nContent-Type: "
                  + type
                  + "\r\n"
                  + "X-Content-Type-Options: nosniff\r\n"
                  + "Cache-Control: no-store\r\n"
                  + "Referrer-Policy: no-referrer\r\n"
                  + "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';"
                  + " frame-ancestors 'none'\r\n");
      for (Map.Entry<String, String> e : extra.entrySet())
        h.append(e.getKey()).append(": ").append(e.getValue()).append("\r\n");
      h.append("\r\n");
      out.write(h.toString().getBytes(StandardCharsets.UTF_8));
      return out;
    }
  }

  static String line(InputStream in) throws IOException {
    ByteArrayOutputStream b = new ByteArrayOutputStream();
    int c;
    while ((c = in.read()) != -1) {
      if (c == 10) {
        byte[] a = b.toByteArray();
        if (a.length == 0 || a[a.length - 1] != 13) throw new BadRequest("CRLF required");
        return new String(a, 0, a.length - 1, StandardCharsets.US_ASCII);
      }
      b.write(c);
      if (b.size() > 8192) throw new BadRequest("Line too long");
    }
    throw new EOFException();
  }

  static String decode(String s) throws IOException {
    try {
      return URLDecoder.decode(s, "UTF-8");
    } catch (IllegalArgumentException e) {
      throw new BadRequest("Encoding");
    }
  }

  static final class Limited extends InputStream {
    final InputStream in;
    long remaining;

    Limited(InputStream in, long n) {
      this.in = in;
      remaining = n;
    }

    public int read() throws IOException {
      if (remaining == 0) return -1;
      int c = in.read();
      if (c < 0) throw new EOFException();
      remaining--;
      return c;
    }

    public int read(byte[] b, int off, int len) throws IOException {
      if (len == 0) return 0;
      if (remaining == 0) return -1;
      int n = in.read(b, off, (int) Math.min(remaining, len));
      if (n < 0) throw new EOFException();
      remaining -= n;
      return n;
    }
  }

  static final class Chunked extends InputStream {
    final InputStream in;
    long remaining;
    boolean end, separator;

    Chunked(InputStream in) {
      this.in = in;
    }

    private boolean next() throws IOException {
      if (end) return false;
      if (remaining > 0) return true;
      if (separator && !line(in).isEmpty()) throw new BadRequest("Chunk separator");
      try {
        remaining = Long.parseLong(line(in).split(";", 2)[0], 16);
      } catch (NumberFormatException e) {
        throw new BadRequest("Chunk size");
      }
      if (remaining < 0) throw new BadRequest("Chunk size");
      separator = true;
      if (remaining == 0) {
        int total = 0;
        String l;
        while (!(l = line(in)).isEmpty()) {
          total += l.length();
          if (total > 8192) throw new BadRequest("Trailers");
        }
        end = true;
        return false;
      }
      return true;
    }

    public int read() throws IOException {
      byte[] b = new byte[1];
      return read(b, 0, 1) < 0 ? -1 : b[0] & 255;
    }

    public int read(byte[] b, int off, int len) throws IOException {
      if (len == 0) return 0;
      if (!next()) return -1;
      int n = in.read(b, off, (int) Math.min(remaining, len));
      if (n < 0) throw new EOFException();
      remaining -= n;
      return n;
    }
  }

  @Override
  public void close() {
    closed = true;
    try {
      server.close();
    } catch (IOException ignored) {
    }
    for (Socket s : sockets)
      try {
        s.close();
      } catch (IOException ignored) {
      }
    workers.shutdownNow();
  }
}
