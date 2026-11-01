// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/automation_server.h"

#include <optional>
#include <utility>

#include "base/json/json_reader.h"
#include "base/json/json_writer.h"
#include "base/logging.h"
#include "base/strings/utf_string_conversions.h"
#include "base/time/time.h"
#include "content/public/browser/render_frame_host.h"
#include "net/base/ip_address.h"
#include "net/base/ip_endpoint.h"
#include "net/base/net_errors.h"
#include "net/traffic_annotation/network_traffic_annotation.h"

namespace env {
namespace {

constexpr size_t kMaxBuffer = 1 << 20;   // a request larger than this is a bug

}  // namespace

std::u16string BuildOpsScript(const base::ListValue& ops) {
  std::string payload;
  base::JSONWriter::Write(ops, &payload);
  return base::UTF8ToUTF16(
      "(() => { const ops = " + payload +
      "; const out = []; for (const op of ops) { try {"
      " if (op.click !== undefined) { const el = document.querySelector(op.click);"
      "   if (el) { el.click(); out.push({click: true}); } else out.push({click: false}); }"
      " else if (op.read !== undefined) { const el = document.querySelector(op.read);"
      "   out.push({read: el ? (el.innerText || el.value || '').slice(0, op.limit || 200)"
      "                      : null}); }"
      " else if (op.type !== undefined) {"
      "   const el = document.querySelector(op.type.selector);"
      "   if (el) { el.focus(); el.value = op.type.text;"
      "     el.dispatchEvent(new Event('input', {bubbles: true})); out.push({type: true}); }"
      "   else out.push({type: false}); }"
      " else if (op.observe !== undefined) {"
      "   const nodes = Array.from(document.querySelectorAll('a[href],button,input,select'))"
      "     .slice(0, op.limit || 20);"
      "   out.push({observe: nodes.map((el) => ({ text:"
      "     (el.innerText || el.value || '').trim().slice(0, 30) }))}); }"
      " else { out.push({error: 'unknown op'}); }"
      " } catch (e) { out.push({error: String(e).slice(0, 120)}); } }"
      " return JSON.stringify({url: location.href, results: out}); })()");
}

void RunOpsOnFrame(content::RenderFrameHost* frame,
                   base::ListValue ops,
                   base::OnceCallback<void(base::Value)> callback) {
  if (!frame) {
    std::move(callback).Run(base::Value());
    return;
  }
  frame->ExecuteJavaScriptInIsolatedWorld(
      BuildOpsScript(ops),
      base::BindOnce(
          [](base::OnceCallback<void(base::Value)> done, base::Value value) {
            std::move(done).Run(std::move(value));
          },
          std::move(callback)),
      /*world_id=*/1);
}

AutomationServer::AutomationServer(Profile* profile,
                                   mojo::Remote<mojom::AutomationTask> task)
    : profile_(profile), task_(std::move(task)) {}

AutomationServer::~AutomationServer() {
  Stop();
}

bool AutomationServer::Start(int port, const std::string& token) {
  token_ = token;
  auto socket = std::make_unique<net::TCPServerSocket>(/*net_log=*/nullptr,
                                                       net::NetLogSource());
  const net::IPEndPoint address(net::IPAddress::IPv4Localhost(), port);
  const int result =
      socket->Listen(address, /*backlog=*/4, /*ipv6_only=*/std::nullopt);
  if (result != net::OK) {
    LOG(ERROR) << "ENVAUTO: listen on 127.0.0.1:" << port
               << " failed: " << net::ErrorToString(result);
    return false;
  }
  net::IPEndPoint bound;
  if (socket->GetLocalAddress(&bound) == net::OK) {
    port_ = bound.port();
  } else {
    port_ = port;
  }
  server_socket_ = std::move(socket);
  LOG(ERROR) << "ENVAUTO: listening on 127.0.0.1:" << port_;
  DoAccept();
  return true;
}

void AutomationServer::Stop() {
  client_socket_.reset();
  server_socket_.reset();
}

void AutomationServer::DoAccept() {
  if (!server_socket_) {
    return;
  }
  net::IPEndPoint peer;
  int result = server_socket_->Accept(
      &client_socket_,
      base::BindOnce(&AutomationServer::OnAccepted, weak_factory_.GetWeakPtr()),
      &peer);
  if (result != net::ERR_IO_PENDING) {
    OnAccepted(result);
  }
}

void AutomationServer::OnAccepted(int result) {
  if (result != net::OK || !client_socket_) {
    LOG(ERROR) << "ENVAUTO: accept failed: " << net::ErrorToString(result);
    server_socket_.reset();
    return;
  }
  LOG(ERROR) << "ENVAUTO: client connected";
  buffer_.clear();
  ReadMore();
}

void AutomationServer::ReadMore() {
  if (!client_socket_) {
    return;
  }
  auto buffer = base::MakeRefCounted<net::IOBufferWithSize>(8192);
  net::IOBuffer* raw = buffer.get();
  int result = client_socket_->Read(
      raw, raw->size(),
      base::BindOnce(&AutomationServer::OnRead, weak_factory_.GetWeakPtr(),
                     buffer));
  if (result != net::ERR_IO_PENDING) {
    OnRead(std::move(buffer), result);
  }
}

void AutomationServer::OnRead(scoped_refptr<net::IOBufferWithSize> buffer,
                              int result) {
  if (result <= 0) {
    LOG(ERROR) << "ENVAUTO: client disconnected (" << net::ErrorToString(result)
               << ")";
    client_socket_.reset();
    buffer_.clear();
    // Re-arm: without this only the first connection is ever served (measured: later
    // requests timed out).
    DoAccept();
    return;
  }
  buffer_.append(buffer->data(), static_cast<size_t>(result));
  // Newline-delimited JSON: handle every complete line, keep the remainder for the
  // next read.
  size_t newline = 0;
  while ((newline = buffer_.find('\n')) != std::string::npos) {
    std::string line = buffer_.substr(0, newline);
    buffer_.erase(0, newline + 1);
    if (!line.empty()) {
      HandleLine(line);
    }
  }
  if (buffer_.size() > kMaxBuffer) {
    LOG(ERROR) << "ENVAUTO: request buffer overflow, dropping the client";
    client_socket_.reset();
    buffer_.clear();
    return;
  }
  ReadMore();
}

void AutomationServer::HandleLine(const std::string& line) {
  std::optional<base::Value> parsed = base::JSONReader::Read(line, base::JSON_PARSE_RFC);
  if (!parsed || !parsed->is_dict()) {
    return;
  }
  const base::DictValue& request = parsed->GetDict();
  const std::string* token = request.FindString("token");
  if (!token || *token != token_) {
    LOG(ERROR) << "ENVAUTO: rejected a request with a bad token";
    return;
  }
  const std::string* env_id = request.FindString("env");
  const base::ListValue* raw_ops = request.FindList("ops");
  if (!env_id || !raw_ops) {
    return;
  }

  // JSON ops -> typed mojom ops. `clickTrusted` runs on its own path (browser-side event
  // forwarding), so those selectors are collected separately.
  std::vector<mojom::OpPtr> ops;
  std::vector<std::string> trusted_selectors;
  for (const base::Value& item : *raw_ops) {
    if (!item.is_dict()) {
      continue;
    }
    const base::DictValue& dict = item.GetDict();
    mojom::OpPtr op = mojom::Op::New();
    if (const std::string* selector = dict.FindString("click")) {
      op->kind = "click";
      op->selector = *selector;
    } else if (const std::string* selector2 = dict.FindString("read")) {
      op->kind = "read";
      op->selector = *selector2;
    } else if (const base::DictValue* type = dict.FindDict("type")) {
      op->kind = "type";
      if (const std::string* selector3 = type->FindString("selector")) {
        op->selector = *selector3;
      }
      if (const std::string* text = type->FindString("text")) {
        op->text = *text;
      }
    } else if (const std::string* trusted = dict.FindString("clickTrusted")) {
      op->kind = "clickTrusted";
      trusted_selectors.push_back(*trusted);
      continue;
    } else if (dict.contains("observe")) {
      op->kind = "observe";
    } else {
      op->kind = "unknown";
    }
    op->limit = dict.FindInt("limit").value_or(200);
    ops.push_back(std::move(op));
  }

  busy_ = true;
  if (!trusted_selectors.empty()) {
    task_->ClickTrusted(
        *env_id, std::move(trusted_selectors),
        base::BindOnce(
            [](base::WeakPtr<AutomationServer> self, mojom::TaskResultPtr result) {
              if (!self || !self->client_socket_) {
                return;
              }
              base::DictValue reply;
              reply.Set("ok", result->ok);
              if (!result->error.empty()) {
                reply.Set("error", result->error);
              }
              reply.Set("ms", result->ms);
              base::ListValue op_results;
              for (const mojom::OpResultPtr& op_result : result->results) {
                base::DictValue entry;
                entry.Set("ok", op_result->ok);
                entry.Set("detail", op_result->detail);
                op_results.Append(std::move(entry));
              }
              reply.Set("results", std::move(op_results));
              std::string body;
              base::JSONWriter::Write(reply, &body);
              body += "\n";
              auto buffer = base::MakeRefCounted<net::StringIOBuffer>(body);
              const net::NetworkTrafficAnnotationTag traffic =
                  MISSING_TRAFFIC_ANNOTATION;
              self->client_socket_->Write(buffer.get(), buffer->size(),
                                          base::BindOnce([](int) {}), traffic);
              self->busy_ = false;
              self->client_socket_.reset();
              self->DoAccept();
            },
            weak_factory_.GetWeakPtr()));
    return;
  }
  task_->RunTask(
      *env_id, std::move(ops),
      base::BindOnce(
          [](base::WeakPtr<AutomationServer> self, mojom::TaskResultPtr result) {
            if (!self || !self->client_socket_) {
              return;
            }
            base::DictValue reply;
            reply.Set("ok", result->ok);
            if (!result->error.empty()) {
              reply.Set("error", result->error);
            }
            reply.Set("ms", result->ms);
            reply.Set("url", result->page_url);
            base::ListValue op_results;
            for (const mojom::OpResultPtr& op_result : result->results) {
              base::DictValue entry;
              entry.Set("ok", op_result->ok);
              entry.Set("detail", op_result->detail);
              op_results.Append(std::move(entry));
            }
            reply.Set("results", std::move(op_results));
            std::string body;
            base::JSONWriter::Write(reply, &body);
            body += "\n";
            auto buffer = base::MakeRefCounted<net::StringIOBuffer>(body);
            const net::NetworkTrafficAnnotationTag traffic =
                MISSING_TRAFFIC_ANNOTATION;
            self->client_socket_->Write(buffer.get(), buffer->size(),
                                        base::BindOnce([](int) {}), traffic);
            self->busy_ = false;
            self->client_socket_.reset();
            self->DoAccept();
          },
          weak_factory_.GetWeakPtr()));
}

}  // namespace env
