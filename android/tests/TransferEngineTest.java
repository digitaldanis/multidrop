package de.digitaldanis.multidrop;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.*;
import java.util.concurrent.*;
import javax.net.*;
import javax.net.ssl.*;
import org.json.*;

public class TransferEngineTest {
  static void check(boolean condition, String message) {
    if (!condition) throw new AssertionError(message);
  }

  static class Host implements TransferEngine.Host {
    volatile boolean accept = true;
    int commits, aborts;
    final Map<String, byte[]> files = new ConcurrentHashMap<>();

    public boolean accept(String sender, List<TransferEngine.Item> files) {
      return accept;
    }

    public TransferEngine.Sink create(TransferEngine.Item item) {
      return new TransferEngine.Sink() {
        final ByteArrayOutputStream data = new ByteArrayOutputStream();

        public OutputStream output() {
          return data;
        }

        public void commit() {
          files.put(item.name, data.toByteArray());
          commits++;
        }

        public void abort() {
          aborts++;
        }
      };
    }

    public void event(String message, long done, long total) {}

    public void completed(String name, boolean incoming) {}
  }

  static class Reply {
    int code;
    String body;

    Reply(HttpURLConnection c) throws Exception {
      code = c.getResponseCode();
      InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
      body = in == null ? "" : new String(in.readAllBytes(), StandardCharsets.UTF_8);
      c.disconnect();
    }
  }

  static Reply post(TransferEngine e, String path, String body) throws Exception {
    HttpURLConnection c =
        (HttpURLConnection)
            new URL("http://127.0.0.1:" + e.port() + TransferEngine.API + path).openConnection();
    c.setRequestMethod("POST");
    c.setDoOutput(true);
    c.setConnectTimeout(2000);
    c.setReadTimeout(2000);
    try (OutputStream o = c.getOutputStream()) {
      o.write(body.getBytes(StandardCharsets.UTF_8));
    }
    return new Reply(c);
  }

  static JSONObject offer(TransferEngine sender, String name, long size, String hash)
      throws Exception {
    JSONObject file =
        new JSONObject()
            .put("id", "one")
            .put("fileName", name)
            .put("size", size)
            .put("fileType", "text/plain");
    if (hash != null) file.put("sha256", hash);
    return new JSONObject()
        .put("info", sender.info())
        .put("files", new JSONObject().put("one", file));
  }

  static String query(JSONObject session) throws Exception {
    return "?sessionId="
        + session.getString("sessionId")
        + "&fileId=one&token="
        + session.getJSONObject("files").getString("one");
  }

  static void localSend() throws Exception {
    Host senderHost = new Host(), receiverHost = new Host();
    try (TransferEngine sender = new TransferEngine("Sender", "sender", false, senderHost);
        TransferEngine receiver = new TransferEngine("Receiver", "receiver", false, receiverHost)) {
      sender.start(ServerSocketFactory.getDefault(), 0, false);
      receiver.start(ServerSocketFactory.getDefault(), 0, false);
      TransferEngine.Peer peer = new TransferEngine.Peer(receiver.info(), "127.0.0.1");
      byte[] bytes = "Hello Android – Grüße!".getBytes(StandardCharsets.UTF_8);
      String hash = TransferEngine.hex(MessageDigest.getInstance("SHA-256").digest(bytes));
      sender.send(
          peer,
          Arrays.asList(
              new TransferEngine.Item(
                  "f",
                  "hello.txt",
                  "text/plain",
                  bytes.length,
                  hash,
                  () -> new ByteArrayInputStream(bytes))),
          "");
      check(Arrays.equals(bytes, receiverHost.files.get("hello.txt")), "send/receive with SHA-256");
      check(receiverHost.commits == 1, "atomic commit");
      receiverHost.accept = false;
      try {
        sender.send(
            peer,
            Arrays.asList(
                new TransferEngine.Item(
                    "f",
                    "no.txt",
                    "text/plain",
                    0,
                    null,
                    () -> new ByteArrayInputStream(new byte[0]))),
            "");
        throw new AssertionError("decline accepted");
      } catch (IOException e) {
        check(e.getMessage().contains("abgelehnt"), "decline error");
      }
      receiverHost.accept = true;
      for (String name :
          new String[] {"../evil", "/absolute", "folder/file", "a\\b", "..", "a\r\nb"})
        check(
            post(receiver, "/prepare-upload", offer(sender, name, 0, null).toString()).code == 400,
            "unsafe filename " + name);
      check(
          post(receiver, "/prepare-upload", offer(sender, "negative", -1, null).toString()).code
              == 400,
          "negative size");
      check(
          post(receiver, "/prepare-upload", offer(sender, "bad-hash", 1, "no").toString()).code
              == 400,
          "bad hash");
      JSONObject session =
          new JSONObject(
              post(
                      receiver,
                      "/prepare-upload",
                      offer(sender, "checksum.txt", 3, "0".repeat(64)).toString())
                  .body);
      check(
          post(receiver, "/prepare-upload", offer(sender, "busy.txt", 0, null).toString()).code
              == 409,
          "busy session");
      check(post(receiver, "/upload", "abc").code == 403, "missing tokens rejected");
      check(
          post(receiver, "/upload" + query(session) + "x", "abc").code == 403,
          "wrong token rejected");
      check(post(receiver, "/upload" + query(session), "abc").code == 422, "checksum mismatch");
      check(receiverHost.aborts == 1, "failed checksum abort");
      post(receiver, "/cancel?sessionId=" + session.getString("sessionId"), "");
      check(
          post(receiver, "/upload" + query(session), "abc").code == 403,
          "cancel invalidates token");
      session =
          new JSONObject(
              post(receiver, "/prepare-upload", offer(sender, "size.txt", 2, null).toString())
                  .body);
      check(post(receiver, "/upload" + query(session), "abc").code == 400, "oversized upload");
      check(receiverHost.aborts == 2, "oversized abort");
      check(post(receiver, "/upload" + query(session), "a").code == 400, "undersized upload");
      check(receiverHost.aborts == 3, "undersized abort");
      check(
          post(receiver, "/upload" + query(session), "ab").code == 200, "retry same accepted file");
      check(post(receiver, "/upload" + query(session), "ab").code == 403, "token cannot replay");
      session =
          new JSONObject(
              post(receiver, "/prepare-upload", offer(sender, "chunked.txt", 3, null).toString())
                  .body);
      HttpURLConnection chunk =
          (HttpURLConnection)
              new URL(
                      "http://127.0.0.1:"
                          + receiver.port()
                          + TransferEngine.API
                          + "/upload"
                          + query(session))
                  .openConnection();
      chunk.setDoOutput(true);
      chunk.setRequestMethod("POST");
      chunk.setChunkedStreamingMode(2);
      try (OutputStream out = chunk.getOutputStream()) {
        out.write(new byte[] {1, 2, 3});
      }
      check(new Reply(chunk).code == 200, "chunked upload");
      int browser =
          sender.shareBrowser(
              Arrays.asList(
                  new TransferEngine.Item(
                      "x",
                      "<hello>.txt",
                      "text/plain",
                      bytes.length,
                      null,
                      () -> new ByteArrayInputStream(bytes))));
      String base = "http://127.0.0.1:" + browser;
      Reply unauthorized =
          new Reply((HttpURLConnection) new URL(base + "/guess/").openConnection());
      check(unauthorized.code == 404, "browser token required");
      Reply page =
          new Reply((HttpURLConnection) new URL(base + sender.browserPath()).openConnection());
      check(page.body.contains("&lt;hello&gt;"), "HTML escaping");
      Reply download =
          new Reply(
              (HttpURLConnection) new URL(base + sender.browserPath() + "0").openConnection());
      check(download.body.equals(new String(bytes, StandardCharsets.UTF_8)), "browser download");
      sender.stopBrowser();
      System.out.println(
          "PASS: LocalSend roundtrip, consent, session busy, size/hash validation, retries, replay,"
              + " cancel, chunking, browser capability link and HTML escaping");
    }
  }

  static void tls() throws Exception {
    KeyStore ks = KeyStore.getInstance("PKCS12");
    try (InputStream in = new FileInputStream("build/test-tls.p12")) {
      ks.load(in, "testpass".toCharArray());
    }
    KeyManagerFactory km = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
    km.init(ks, "testpass".toCharArray());
    SSLContext ssl = SSLContext.getInstance("TLS");
    ssl.init(km.getKeyManagers(), null, null);
    String fingerprint =
        TransferEngine.hex(
            MessageDigest.getInstance("SHA-256").digest(ks.getCertificate("test").getEncoded()));
    Host h = new Host();
    try (TransferEngine e = new TransferEngine("TLS", fingerprint, true, h)) {
      e.start(ssl.getServerSocketFactory(), 0, false);
      TransferEngine.Peer peer = new TransferEngine.Peer(e.info(), "127.0.0.1");
      HttpURLConnection c = TransferEngine.connection(peer, "/info", 3000);
      check(new Reply(c).code == 200, "pinned TLS succeeds");
      JSONObject wrong = e.info().put("fingerprint", "0".repeat(64));
      peer = new TransferEngine.Peer(wrong, "127.0.0.1");
      try {
        new Reply(TransferEngine.connection(peer, "/info", 3000));
        throw new AssertionError("untrusted cert accepted");
      } catch (SSLHandshakeException expected) {
      }
    }
    System.out.println(
        "PASS: HTTPS self-signed certificate accepted only with matching SHA-256 pin");
  }

  static void malformedHttp() throws Exception {
    try (LocalHttp server =
        new LocalHttp(ServerSocketFactory.getDefault(), 0, (r, s) -> s.text(200, r.json()))) {
      for (String headers :
          new String[] {
            "Content-Length: 1\r\nContent-Length: 2\r\n",
            "Content-Length: 1\r\nTransfer-Encoding: chunked\r\n",
            "Content-Length: -1\r\n",
            "Transfer-Encoding: gzip\r\n"
          })
        try (Socket sock = new Socket("127.0.0.1", server.port())) {
          sock.setSoTimeout(2000);
          sock.getOutputStream()
              .write(
                  ("POST / HTTP/1.1\r\nHost: localhost\r\n" + headers + "\r\n")
                      .getBytes(StandardCharsets.US_ASCII));
          String response =
              new String(sock.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
          check(response.startsWith("HTTP/1.1 400"), "ambiguous request rejected");
        }
    }
    System.out.println(
        "PASS: HTTP duplicate/ambiguous/negative lengths and unsupported encodings rejected");
  }

  public static void main(String[] args) throws Exception {
    localSend();
    tls();
    malformedHttp();
  }
}
