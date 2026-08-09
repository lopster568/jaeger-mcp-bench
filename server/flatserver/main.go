// flatserver is the "flat" arm of the arm2-progressive-disclosure bench: the
// naive single-tool MCP integration that a team wraps around jaeger-query's
// search API on a first pass, with no drill-down tiers. See
// docs/arm2-design.md for the full experiment design ("flat" vs "tiered").
//
// It exposes exactly one tool, get_trace_data, which fetches complete,
// unsummarized span dumps from jaeger-query's classic /api/traces HTTP API.
//
// Run:
//
//	flatserver --jaeger-url=http://localhost:16686 --listen=0.0.0.0:8090
//
// Serves MCP over streamable HTTP (mounted at /mcp), not stdio - transport
// parity with the tiered arm (Jaeger's own MCP server, served at
// /api/ai/mcp/ on the jaeger-query port) is a registered design constraint,
// since arm 2 varies only the tool inventory shape, not the transport.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// mcpPath is where the streamable HTTP handler is mounted. Kept simple and
// stable since the harness's MCP client config just needs a fixed URL to
// point at (see docs/arm2-design.md's harness/mcp_config.py item).
const mcpPath = "/mcp"

// mcpSessionTimeout mirrors jaeger-query's own MCP server
// (cmd/jaeger/internal/extension/jaegerquery/internal/mcptools/config.go in
// jaegertracing/jaeger v2.20.0) so both arms behave identically at the
// transport layer and only the tool inventory differs.
const mcpSessionTimeout = 5 * time.Minute

func main() {
	jaegerURL := flag.String("jaeger-url", "http://localhost:16686", "Jaeger query API base URL")
	listen := flag.String("listen", "0.0.0.0:8090", "host:port to listen on for streamable HTTP MCP")
	flag.Parse()

	client := newJaegerClient(*jaegerURL)
	// Everything in the initialize payload (name, version, instructions) is
	// model-visible. It must carry no experiment vocabulary ("naive",
	// "baseline", "arm") - priming the model under test would confound the
	// comparison. The instructions open with the same one-line domain framing
	// as the tiered arm's INSTRUCTIONS.md and then stop: strategy guidance is
	// the tiered arm's treatment, not this arm's.
	server := mcp.NewServer(
		&mcp.Implementation{
			Name:    "jaeger-traces",
			Version: "1.0.0",
		},
		&mcp.ServerOptions{
			Instructions: "Jaeger is a distributed tracing backend. A trace is a tree of spans representing a " +
				"request or workflow within layers or components of a system. " +
				"Use get_trace_data to retrieve traces for a service.",
		},
	)
	registerTools(server, client)

	// JSONResponse: false (SSE) and Stateless: false mirror jaeger-query's
	// own streamable HTTP wiring (mcptools.WrapHTTP in jaeger v2.20.0) so the
	// two arms are transport-identical and the only experimental variable is
	// the tool inventory, per docs/arm2-design.md.
	handler := mcp.NewStreamableHTTPHandler(
		func(*http.Request) *mcp.Server { return server },
		&mcp.StreamableHTTPOptions{
			JSONResponse:   false,
			Stateless:      false,
			SessionTimeout: mcpSessionTimeout,
		},
	)

	mux := http.NewServeMux()
	mux.Handle(mcpPath, handler)
	mux.Handle(mcpPath+"/", handler)

	httpServer := &http.Server{
		Addr:              *listen,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdownCtx)
	}()

	log.Printf("flatserver listening on %s%s (jaeger-url=%s)", *listen, mcpPath, *jaegerURL)
	if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("httpServer.ListenAndServe: %v", err)
	}
}
