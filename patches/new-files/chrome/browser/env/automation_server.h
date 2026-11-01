// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_AUTOMATION_SERVER_H_
#define CHROME_BROWSER_ENV_AUTOMATION_SERVER_H_

#include <memory>
#include <string>

#include "base/functional/callback.h"
#include "base/memory/weak_ptr.h"
#include "base/values.h"
#include "chrome/browser/env/automation_task.mojom.h"
#include "mojo/public/cpp/bindings/remote.h"
#include "net/socket/tcp_server_socket.h"
#include "net/base/io_buffer.h"
#include "net/socket/stream_socket.h"

class Profile;
namespace content {
class RenderFrameHost;
}

namespace env {

// A loopback socket for the automation bridge.
//
// Measured motivation (design doc §31): the op-list executor runs at 0.02 ms/op inside the
// browser, but the file-channel transport costs 250-300 ms per call, so the transport is the
// whole bottleneck. This server keeps one connection open and answers newline-delimited JSON
// requests, which takes the transport out of the picture.
//
// Protocol: one JSON object per line, token first.
//   -> {"token":"...","env":"fb-008","ops":[{"click":"button"},{"read":"h1"}]}
//   <- {"ok":true,"ms":0.4,"results":[...],"url":"..."}
//   <- {"ok":false,"error":"..."}
class AutomationServer {
 public:
  // The transport talks to the executor only through this typed interface.
  AutomationServer(Profile* profile, mojo::Remote<mojom::AutomationTask> task);
  AutomationServer(const AutomationServer&) = delete;
  AutomationServer& operator=(const AutomationServer&) = delete;
  ~AutomationServer();

  // Starts listening on 127.0.0.1:`port`. Returns false when the socket cannot be bound.
  bool Start(int port, const std::string& token);
  void Stop();
  bool listening() const { return server_socket_ != nullptr; }
  int port() const { return port_; }

 private:
  void DoAccept();
  void OnAccepted(int result);
  void ReadMore();
  void OnRead(scoped_refptr<net::IOBufferWithSize> buffer, int result);
  void HandleLine(const std::string& line);

  raw_ptr<Profile> profile_;
  mojo::Remote<mojom::AutomationTask> task_;
  std::unique_ptr<net::TCPServerSocket> server_socket_;
  std::unique_ptr<net::StreamSocket> client_socket_;
  std::string buffer_;
  std::string token_;
  int port_ = 0;
  bool busy_ = false;

  base::WeakPtrFactory<AutomationServer> weak_factory_{this};
};

// Builds the isolated-world script that executes an op list, and runs it on `frame`.
// Shared with EnvManager's file-channel `task` command so both transports behave the same.
std::u16string BuildOpsScript(const base::ListValue& ops);
void RunOpsOnFrame(content::RenderFrameHost* frame,
                   base::ListValue ops,
                   base::OnceCallback<void(base::Value)> callback);

}  // namespace env

#endif  // CHROME_BROWSER_ENV_AUTOMATION_SERVER_H_
