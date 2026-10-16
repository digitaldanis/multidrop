package de.digitaldanis.multidrop;

import java.io.*;
import java.nio.file.*;
import java.util.*;
import javax.net.ServerSocketFactory;
import org.json.*;

/** CLI harness for testing the Android protocol engine against the real desktop backend. */
public class InteropHarness {
  public static void main(String[] args) throws Exception {
    Path dir = Paths.get(args[0]);
    Files.createDirectories(dir);
    TransferEngine.Host host =
        new TransferEngine.Host() {
          public boolean accept(String sender, List<TransferEngine.Item> files) {
            return true;
          }

          public TransferEngine.Sink create(TransferEngine.Item file) throws Exception {
            Path part = Files.createTempFile(dir, "incoming-", ".part");
            return new TransferEngine.Sink() {
              public OutputStream output() throws Exception {
                return Files.newOutputStream(part);
              }

              public void commit() throws Exception {
                Files.move(part, dir.resolve(file.name));
              }

              public void abort() {
                try {
                  Files.deleteIfExists(part);
                } catch (IOException ignored) {
                }
              }
            };
          }

          public void event(String message, long done, long total) {}

          public void completed(String name, boolean received) {}
        };
    try (TransferEngine engine = new TransferEngine("Android test", "android-test", false, host)) {
      engine.start(ServerSocketFactory.getDefault(), 0, false);
      System.out.println(engine.port());
      System.out.flush();
      if (args.length > 1) {
        JSONObject info =
            new JSONObject()
                .put("alias", "Desktop")
                .put("fingerprint", "desktop-test")
                .put("port", Integer.parseInt(args[1]))
                .put("protocol", "http");
        Path file = dir.resolve("from-android.txt");
        byte[] bytes = "Android → Desktop ✓".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        Files.write(file, bytes);
        engine.send(
            new TransferEngine.Peer(info, "127.0.0.1"),
            Collections.singletonList(
                new TransferEngine.Item(
                    "one",
                    file.getFileName().toString(),
                    "text/plain",
                    bytes.length,
                    null,
                    () -> Files.newInputStream(file))),
            "");
        System.out.println("SENT");
        System.out.flush();
      }
      System.in.read();
    }
  }
}
