// jaeger-mcp-bench-server is a research-prototype MCP server that exposes
// Jaeger SPM metrics in two switchable output formats (summary | series).
// Used to A/B benchmark which format better serves an LLM agent.
//
// Run:
//   jaeger-mcp-bench-server --jaeger-url=http://localhost:16686 --format=summary
//
// Talks MCP over stdio. Boot via claude/gemini CLI's --mcp-config.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func main() {
	jaegerURL := flag.String("jaeger-url", "http://localhost:16686", "Jaeger query API base URL")
	format := flag.String("format", "summary", "Output format: summary | series")
	flag.Parse()

	if *format != "summary" && *format != "series" {
		fmt.Fprintf(os.Stderr, "invalid --format=%q (want summary|series)\n", *format)
		os.Exit(2)
	}

	client := newJaegerClient(*jaegerURL)
	server := mcp.NewServer(
		&mcp.Implementation{
			Name:    "jaeger-mcp-bench",
			Version: "0.1.0-research",
		},
		&mcp.ServerOptions{
			Instructions: fmt.Sprintf(
				"Research prototype: Jaeger SPM metrics in %q format.",
				*format,
			),
		},
	)

	registerMetricsTools(server, client, *format)

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatalf("server.Run: %v", err)
	}
}
