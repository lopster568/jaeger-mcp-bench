// tools.go registers the flat arm's one and only MCP tool.

package main

import (
	"context"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// getTraceDataDescription states plainly what the tool returns and stops
// there: no drill-down guidance (that is the tiered arm's treatment) and no
// size warnings (a nudge toward small fetches would shape the behavior under
// test). See docs/arm2-design.md.
const getTraceDataDescription = "Search Jaeger for traces of a service and return every matching trace as a " +
	"complete span dump: every span, with all its attributes, events (logs), links (references), timing, and " +
	"status, exactly as jaeger-query returns them. No summarization or truncation is applied beyond the " +
	"caller-supplied 'limit' (maximum number of traces returned)."

func registerTools(s *mcp.Server, client *jaegerClient) {
	mcp.AddTool(s, &mcp.Tool{
		Name:        "get_trace_data",
		Description: getTraceDataDescription,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in getTraceDataInput) (*mcp.CallToolResult, any, error) {
		q, err := tracesQuery(time.Now(), in)
		if err != nil {
			return errResult(err), nil, nil
		}
		body, err := client.fetchTraces(ctx, q)
		if err != nil {
			return errResult(err), nil, nil
		}
		return &mcp.CallToolResult{
			Content: []mcp.Content{&mcp.TextContent{Text: string(body)}},
		}, nil, nil
	})
}

func errResult(err error) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		IsError: true,
		Content: []mcp.Content{&mcp.TextContent{Text: err.Error()}},
	}
}
