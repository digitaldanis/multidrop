package de.digitaldanis.multidrop;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.cert.*;
import java.util.*;
import java.util.concurrent.*;
import javax.net.ServerSocketFactory;
import javax.net.ssl.*;
import org.json.*;

/** Platform-independent LocalSend v2 engine; storage and consent are supplied by Android. */
public final class TransferEngine implements AutoCloseable {
  public static final String API = "/api/localsend/v2";

  public interface Source {
    InputStream open() throws Exception;
  }

  public interface Sink {
    OutputStream output() throws Exception;

    void commit() throws Exception;

    void abort();
  }

  public interface Host {
    boolean accept(String sender, List<Item> files) throws Exception;

    Sink create(Item item) throws Exception;

    void event(String message, long done, long total);

    void completed(String name, boolean received);
  }

  public static final class Item {
    public final String id, name, mime, sha256;
    public final long size;
    public final Source source;

    public Item(String id, String name, String mime, long size, String hash, Source source) {
      this.id = id;
      this.name = safeName(name);
      this.mime = mime == null ? "application/octet-stream" : mime;
      this.size = size;
      this.sha256 = hash;
      this.source = source;
      if (size < 0) throw new IllegalArgumentException("Unknown file size");
    }

    JSONObject json() throws JSONException {
      JSONObject j = new JSONObject();
      j.put("id", id);
      j.put("fileName", name);
      j.put("fileType", mime);
      j.put("size", size);
      if (sha256 != null) j.put("sha256", sha256);
      return j;
    }
  }

  public static final class Peer {
    public final String name, address, fingerprint, scheme;
    public final int port;
    public volatile long seen = System.currentTimeMillis();

    Peer(JSONObject j, String address) throws JSONException {
      name = j.getString("alias");
      fingerprint = j.getString("fingerprint");
      this.address = address;
      port = j.optInt("port", 53317);
      scheme = j.optString("protocol", "https");
      if (name.length() > 200
          || fingerprint.length() > 256
          || port < 1
          || port > 65535
          || (!scheme.equals("https") && !scheme.equals("http")))
        throw new JSONException("Invalid peer");
    }

    public String base() {
      return scheme
          + "://"
          + (address.contains(":") ? "[" + address + "]" : address)
          + ":"
          + port
          + API;
    }
  }

  private static final class Session {
    final String id = token(), address;
    final Map<String, Item> items = new HashMap<>();
    final Map<String, String> tokens = new HashMap<>();
    final Set<String> active = new HashSet<>();
    volatile boolean cancelled;
    volatile long touched = System.currentTimeMillis();

    Session(String address) {
      this.address = address;
    }
  }

  private final Host host;
  private final String alias, fingerprint;
  private final boolean tls;
  private final Map<String, Peer> peers = new ConcurrentHashMap<>();
  private final ExecutorService jobs = Executors.newFixedThreadPool(2);
  private LocalHttp server, browser;
  private MulticastSocket multicast;
  private volatile boolean closed;
  private Session session;
  private boolean deciding;
  private volatile String browserToken = "";
  private volatile List<Item> browserFiles = Collections.emptyList();
  private volatile long browserExpiry;
  private volatile HttpURLConnection outgoing;
  private volatile boolean cancelSend;
  private boolean sending;
  private final Set<String> registering = ConcurrentHashMap.newKeySet();

  public TransferEngine(String alias, String fingerprint, boolean tls, Host host) {
    this.alias = alias;
    this.fingerprint = fingerprint;
    this.tls = tls;
    this.host = host;
  }

  public void start(ServerSocketFactory factory, int port, boolean discovery) throws Exception {
    server = new LocalHttp(factory, port, this::handle);
    if (discovery) startDiscovery();
  }

  public int port() {
    return server.port();
  }

  public JSONObject info() throws JSONException {
    JSONObject j = new JSONObject();
    j.put("alias", alias);
    j.put("version", "2.2");
    j.put("deviceModel", "Android");
    j.put("deviceType", "mobile");
    j.put("fingerprint", fingerprint);
    j.put("port", port());
    j.put("protocol", tls ? "https" : "http");
    j.put("download", false);
    return j;
  }

  public List<Peer> peers() {
    long now = System.currentTimeMillis();
    peers.values().removeIf(p -> now - p.seen > 90000);
    List<Peer> list = new ArrayList<>(peers.values());
    list.sort(Comparator.comparing(p -> p.name));
    return list;
  }

  private Peer remember(JSONObject j, String address) throws JSONException {
    Peer peer = new Peer(j, address);
    if (!peer.fingerprint.equals(fingerprint)) {
      if (peers.size() < 256 || peers.containsKey(peer.fingerprint))
        peers.put(peer.fingerprint, peer);
    }
    return peer;
  }

  private void startDiscovery() throws Exception {
    multicast = new MulticastSocket(null);
    multicast.setReuseAddress(true);
    multicast.bind(new InetSocketAddress(53317));
    multicast.setTimeToLive(1);
    multicast.setSoTimeout(1000);
    InetAddress group = InetAddress.getByName("224.0.0.167");
    for (NetworkInterface net : Collections.list(NetworkInterface.getNetworkInterfaces()))
      if (net.isUp() && !net.isLoopback() && net.supportsMulticast())
        try {
          multicast.joinGroup(new InetSocketAddress(group, 53317), net);
        } catch (IOException ignored) {
        }
    Thread listen =
        new Thread(
            () -> {
              byte[] bytes = new byte[16384];
              long next = 0;
              while (!closed)
                try {
                  if (System.currentTimeMillis() > next) {
                    announce();
                    next = System.currentTimeMillis() + 15000;
                  }
                  DatagramPacket p = new DatagramPacket(bytes, bytes.length);
                  multicast.receive(p);
                  JSONObject j =
                      new JSONObject(new String(bytes, 0, p.getLength(), StandardCharsets.UTF_8));
                  if (fingerprint.equals(j.optString("fingerprint"))) continue;
                  Peer peer = remember(j, p.getAddress().getHostAddress());
                  if (j.optBoolean("announce", false)
                      && registering.size() < 32
                      && registering.add(peer.fingerprint))
                    jobs.execute(
                        () -> {
                          try {
                            exchange(peer, "/register", info().toString(), 1500);
                          } catch (Exception ignored) {
                          } finally {
                            registering.remove(peer.fingerprint);
                          }
                        });
                } catch (SocketTimeoutException ignored) {
                } catch (Exception e) {
                  if (closed) break;
                }
            },
            "multidrop-discovery");
    listen.setDaemon(true);
    listen.start();
  }

  public void announce() {
    if (multicast == null || closed) return;
    try {
      byte[] data = info().put("announce", true).toString().getBytes(StandardCharsets.UTF_8);
      synchronized (multicast) {
        for (NetworkInterface net : Collections.list(NetworkInterface.getNetworkInterfaces()))
          if (net.isUp() && !net.isLoopback() && net.supportsMulticast())
            try {
              multicast.setNetworkInterface(net);
              multicast.send(
                  new DatagramPacket(
                      data, data.length, InetAddress.getByName("224.0.0.167"), 53317));
            } catch (IOException ignored) {
            }
      }
    } catch (Exception ignored) {
    }
  }

  private void handle(LocalHttp.Request r, LocalHttp.Response out) throws Exception {
    try {
      if (r.method.equals("GET") && r.path.equals(API + "/info")) {
        out.json(200, info().toString());
        return;
      }
      if (!r.method.equals("POST")) {
        out.text(404, "Not found");
        return;
      }
      switch (r.path) {
        case API + "/register":
          remember(new JSONObject(r.json()), r.remote);
          out.json(200, info().toString());
          return;
        case API + "/prepare-upload":
          prepare(r, out);
          return;
        case API + "/upload":
          receive(r, out);
          return;
        case API + "/cancel":
          synchronized (this) {
            if (session != null
                && session.id.equals(r.query.get("sessionId"))
                && session.address.equals(r.remote)) {
              session.cancelled = true;
              session = null;
            }
          }
          out.text(200, "");
          return;
        default:
          out.text(404, "Not found");
      }
    } catch (JSONException | IllegalArgumentException e) {
      out.text(400, "Invalid transfer metadata");
    }
  }

  private void prepare(LocalHttp.Request r, LocalHttp.Response out) throws Exception {
    JSONObject payload = new JSONObject(r.json());
    Peer sender = remember(payload.getJSONObject("info"), r.remote);
    JSONObject raw = payload.getJSONObject("files");
    if (raw.length() < 1 || raw.length() > 100)
      throw new IllegalArgumentException("1–100 files required");
    List<Item> files = new ArrayList<>();
    Set<String> ids = new HashSet<>();
    for (Iterator<String> it = raw.keys(); it.hasNext(); ) {
      String id = it.next();
      JSONObject f = raw.getJSONObject(id);
      if (!id.equals(f.optString("id", id)) || id.length() > 256 || !ids.add(id))
        throw new IllegalArgumentException("File ID");
      long size = f.getLong("size");
      if (size < 0 || size > 1024L * 1024 * 1024 * 100)
        throw new IllegalArgumentException("File size");
      String hash = f.optString("sha256", "");
      if (!hash.isEmpty() && !hash.matches("(?i)[a-f0-9]{64}"))
        throw new IllegalArgumentException("Hash");
      files.add(
          new Item(
              id,
              f.getString("fileName"),
              f.optString("fileType", "application/octet-stream"),
              size,
              hash.isEmpty() ? null : hash,
              null));
    }
    synchronized (this) {
      if (session != null
          && session.active.isEmpty()
          && System.currentTimeMillis() - session.touched > 300000) session = null;
      if (deciding || session != null) {
        out.text(409, "Busy");
        return;
      }
      deciding = true;
    }
    boolean accept;
    try {
      accept = host.accept(sender.name, files);
    } catch (Exception e) {
      synchronized (this) {
        deciding = false;
      }
      throw e;
    }
    if (!accept || closed) {
      synchronized (this) {
        deciding = false;
      }
      out.text(403, "Declined");
      return;
    }
    Session created = new Session(r.remote);
    JSONObject tokens = new JSONObject();
    for (Item f : files) {
      created.items.put(f.id, f);
      String token = token();
      created.tokens.put(f.id, token);
      tokens.put(f.id, token);
    }
    synchronized (this) {
      session = created;
      deciding = false;
    }
    out.json(200, new JSONObject().put("sessionId", created.id).put("files", tokens).toString());
  }

  private void receive(LocalHttp.Request r, LocalHttp.Response out) throws Exception {
    Session s;
    Item file;
    String id = r.query.get("fileId");
    synchronized (this) {
      s = session;
      if (s == null
          || s.cancelled
          || !s.address.equals(r.remote)
          || !s.id.equals(r.query.get("sessionId"))
          || !Objects.equals(s.tokens.get(id), r.query.get("token"))
          || !s.tokens.containsKey(id)) {
        out.text(403, "Invalid session");
        return;
      }
      file = s.items.get(id);
      if (!s.active.add(id)) {
        out.text(409, "File is uploading");
        return;
      }
    }
    Sink sink = null;
    boolean done = false;
    try {
      sink = host.create(file);
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      long received = 0, last = 0;
      try (OutputStream target = sink.output()) {
        byte[] buffer = new byte[65536];
        int n;
        while ((n = r.body.read(buffer)) != -1) {
          if (s.cancelled || closed) throw new IOException("Cancelled");
          received += n;
          if (received > file.size) throw new IOException("Size exceeded");
          target.write(buffer, 0, n);
          digest.update(buffer, 0, n);
          s.touched = System.currentTimeMillis();
          if (received - last >= 262144) {
            host.event("Empfange " + file.name, received, file.size);
            last = received;
          }
        }
      }
      if (received != file.size) throw new IOException("Incomplete file");
      if (file.sha256 != null && !file.sha256.equalsIgnoreCase(hex(digest.digest()))) {
        out.text(422, "Checksum mismatch");
        return;
      }
      if (s.cancelled || closed) throw new IOException("Cancelled");
      sink.commit();
      done = true;
      host.completed(file.name, true);
      out.text(200, "");
    } catch (IOException e) {
      host.event("Empfang abgebrochen: " + file.name, 0, 0);
      out.text(400, "Incomplete or cancelled upload");
    } finally {
      if (!done && sink != null) sink.abort();
      synchronized (this) {
        s.active.remove(id);
        s.touched = System.currentTimeMillis();
        if (done) {
          s.tokens.remove(id);
          s.items.remove(id);
        }
        if (s.items.isEmpty() && session == s) session = null;
      }
    }
  }

  public void send(Peer peer, List<Item> files, String pin) throws Exception {
    synchronized (this) {
      if (sending) throw new IOException("Ein Versand läuft bereits");
      sending = true;
      cancelSend = false;
    }
    try {
      sendInternal(peer, files, pin);
    } finally {
      synchronized (this) {
        sending = false;
      }
      outgoing = null;
    }
  }

  private void sendInternal(Peer peer, List<Item> files, String pin) throws Exception {
    JSONObject meta = new JSONObject();
    for (Item f : files) meta.put(f.id, f.json());
    host.event("Warte auf " + peer.name, 0, 0);
    String result =
        exchange(
            peer,
            "/prepare-upload" + (pin.isEmpty() ? "" : "?pin=" + url(pin)),
            new JSONObject().put("info", info()).put("files", meta).toString(),
            90000);
    if (result.isEmpty()) {
      host.event("Übertragung abgeschlossen", 0, 0);
      return;
    }
    JSONObject response = new JSONObject(result);
    String sid = response.getString("sessionId");
    JSONObject tokens = response.getJSONObject("files");
    try {
      int count = 0;
      for (Item file : files)
        if (tokens.has(file.id)) {
          if (cancelSend) throw new IOException("Abgebrochen");
          HttpURLConnection conn =
              connection(
                  peer,
                  "/upload?sessionId="
                      + url(sid)
                      + "&fileId="
                      + url(file.id)
                      + "&token="
                      + url(tokens.getString(file.id)),
                  60000);
          outgoing = conn;
          try {
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setFixedLengthStreamingMode(file.size);
            conn.setRequestProperty("Content-Type", "application/octet-stream");
            try (InputStream source = file.source.open();
                OutputStream target = conn.getOutputStream()) {
              byte[] buffer = new byte[65536];
              long sent = 0, last = 0;
              int n;
              while ((n = source.read(buffer)) != -1) {
                if (cancelSend || closed) throw new IOException("Abgebrochen");
                target.write(buffer, 0, n);
                sent += n;
                if (sent - last >= 262144) {
                  host.event("Sende " + file.name, sent, file.size);
                  last = sent;
                }
              }
              if (sent != file.size) throw new IOException("Dateigröße hat sich geändert");
            }
            if (conn.getResponseCode() != 200)
              throw new IOException("Upload: HTTP " + conn.getResponseCode());
            host.completed(file.name, false);
            count++;
          } finally {
            conn.disconnect();
          }
        }
      host.event(
          count
              + " Datei(en) gesendet"
              + (count < files.size() ? " · " + (files.size() - count) + " übersprungen" : ""),
          0,
          0);
    } catch (Exception e) {
      try {
        exchange(peer, "/cancel?sessionId=" + url(sid), "", 2000);
      } catch (Exception ignored) {
      }
      throw e;
    }
  }

  public void cancelSend() {
    cancelSend = true;
    HttpURLConnection c = outgoing;
    if (c != null) c.disconnect();
  }

  private String exchange(Peer peer, String path, String json, int timeout) throws Exception {
    HttpURLConnection conn = connection(peer, path, timeout);
    if (path.startsWith("/prepare")) outgoing = conn;
    try {
      conn.setRequestMethod("POST");
      conn.setDoOutput(true);
      conn.setRequestProperty("Content-Type", "application/json");
      byte[] data = json.getBytes(StandardCharsets.UTF_8);
      conn.setFixedLengthStreamingMode(data.length);
      try (OutputStream out = conn.getOutputStream()) {
        out.write(data);
      }
      int code = conn.getResponseCode();
      if (code == 204) return "";
      if (code != 200)
        throw new IOException(
            code == 401
                ? "PIN erforderlich oder falsch"
                : code == 403
                    ? "Empfänger hat abgelehnt"
                    : code == 409 ? "Empfänger ist beschäftigt" : "HTTP " + code);
      try (InputStream in = conn.getInputStream()) {
        return readSmall(in);
      }
    } finally {
      conn.disconnect();
    }
  }

  private static String readSmall(InputStream in) throws IOException {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    byte[] b = new byte[4096];
    int n;
    while ((n = in.read(b)) != -1) {
      if (out.size() + n > 1048576) throw new IOException("Response too large");
      out.write(b, 0, n);
    }
    return out.toString("UTF-8");
  }

  public static HttpURLConnection connection(Peer peer, String path, int timeout) throws Exception {
    HttpURLConnection conn = (HttpURLConnection) new URL(peer.base() + path).openConnection();
    conn.setConnectTimeout(5000);
    conn.setReadTimeout(timeout);
    conn.setInstanceFollowRedirects(false);
    if (conn instanceof HttpsURLConnection) {
      if (!peer.fingerprint.matches("(?i)[a-f0-9]{64}"))
        throw new IOException("Empfänger hat keinen gültigen TLS-Fingerabdruck");
      TrustManager[] trust = {
        new X509TrustManager() {
          public X509Certificate[] getAcceptedIssuers() {
            return new X509Certificate[0];
          }

          public void checkClientTrusted(X509Certificate[] c, String a)
              throws CertificateException {
            throw new CertificateException();
          }

          public void checkServerTrusted(X509Certificate[] c, String a)
              throws CertificateException {
            try {
              if (c.length == 0
                  || !hex(MessageDigest.getInstance("SHA-256").digest(c[0].getEncoded()))
                      .equalsIgnoreCase(peer.fingerprint))
                throw new CertificateException("Certificate fingerprint mismatch");
            } catch (GeneralSecurityException e) {
              throw new CertificateException(e);
            }
          }
        }
      };
      SSLContext context = SSLContext.getInstance("TLS");
      context.init(null, trust, new SecureRandom());
      HttpsURLConnection secure = (HttpsURLConnection) conn;
      secure.setSSLSocketFactory(context.getSocketFactory());
      secure.setHostnameVerifier((h, s) -> true);
    }
    return conn;
  }

  public synchronized int shareBrowser(List<Item> files) throws IOException {
    stopBrowser();
    browserToken = token();
    browserFiles = new ArrayList<>(files);
    browserExpiry = System.currentTimeMillis() + 15 * 60 * 1000;
    browser = new LocalHttp(ServerSocketFactory.getDefault(), 0, this::browserRequest);
    return browser.port();
  }

  public String browserPath() {
    return "/" + browserToken + "/";
  }

  public boolean browserActive() {
    return browser != null && System.currentTimeMillis() < browserExpiry;
  }

  public synchronized void stopBrowser() {
    browserToken = "";
    browserFiles = Collections.emptyList();
    if (browser != null) browser.close();
    browser = null;
  }

  private void browserRequest(LocalHttp.Request r, LocalHttp.Response response) throws Exception {
    String prefix = browserPath();
    List<Item> files = browserFiles;
    if (!r.method.equals("GET")
        || System.currentTimeMillis() > browserExpiry
        || !r.path.startsWith(prefix)
        || browserToken.isEmpty()) {
      response.text(404, "Link expired or not found");
      return;
    }
    String suffix = r.path.substring(prefix.length());
    if (suffix.isEmpty()) {
      StringBuilder html =
          new StringBuilder(
              "<!doctype html><html lang=de><meta name=viewport"
                  + " content='width=device-width,initial-scale=1'><title>MultiDrop</title><style>body{font:17px"
                  + " system-ui;background:#f5f3fa;color:#252039;max-width:600px;margin:60px"
                  + " auto;padding:24px}a{display:block;padding:22px;background:white;border-radius:16px;margin:12px"
                  + " 0;color:#6443cc;text-decoration:none}small{color:#756d87}</style><h1>MultiDrop</h1><p>Direkt"
                  + " von deinem Android-Gerät.</p><small>Freigabe endet nach 15 Minuten.</small>");
      for (int i = 0; i < files.size(); i++) {
        Item f = files.get(i);
        html.append("<a href='")
            .append(prefix)
            .append(i)
            .append("'>")
            .append(escape(f.name))
            .append("<br><small>")
            .append(f.size)
            .append(" Bytes</small></a>");
      }
      response.bytes(
          200, "text/html; charset=utf-8", html.toString().getBytes(StandardCharsets.UTF_8));
      return;
    }
    int index;
    try {
      index = Integer.parseInt(suffix);
    } catch (NumberFormatException e) {
      response.text(404, "Not found");
      return;
    }
    if (index < 0 || index >= files.size()) {
      response.text(404, "Not found");
      return;
    }
    Item f = files.get(index);
    Map<String, String> headers =
        Collections.singletonMap(
            "Content-Disposition", "attachment; filename*=UTF-8''" + url(f.name));
    try (InputStream source = f.source.open()) {
      OutputStream out = response.begin(200, "application/octet-stream", f.size, headers);
      byte[] b = new byte[65536];
      long left = f.size;
      while (left > 0) {
        int n = source.read(b, 0, (int) Math.min(left, b.length));
        if (n < 0) throw new EOFException();
        out.write(b, 0, n);
        left -= n;
      }
      out.flush();
    }
  }

  public static String safeName(String name) {
    if (name == null
        || name.isEmpty()
        || name.length() > 240
        || name.equals(".")
        || name.equals("..")
        || name.startsWith("/")
        || name.contains("\\")
        || name.contains("/")
        || name.chars().anyMatch(c -> c < 32 || c == 127))
      throw new IllegalArgumentException("Invalid file name");
    return name;
  }

  static String escape(String s) {
    return s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
        .replace("'", "&#39;");
  }

  static String url(String s) throws UnsupportedEncodingException {
    return URLEncoder.encode(s, "UTF-8").replace("+", "%20");
  }

  public static String hex(byte[] bytes) {
    StringBuilder s = new StringBuilder();
    for (byte b : bytes) s.append(String.format(Locale.ROOT, "%02x", b & 255));
    return s.toString();
  }

  private static String token() {
    byte[] bytes = new byte[24];
    new SecureRandom().nextBytes(bytes);
    return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
  }

  @Override
  public void close() {
    closed = true;
    cancelSend();
    synchronized (this) {
      if (session != null) session.cancelled = true;
    }
    if (multicast != null) multicast.close();
    if (server != null) server.close();
    stopBrowser();
    jobs.shutdownNow();
  }
}
