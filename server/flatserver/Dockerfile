# Build context is server/ (the module root) so this can import the shared
# go.mod/go.sum without a second module. See compose/docker-compose.yml's
# flatserver service.
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /flatserver ./flatserver

FROM gcr.io/distroless/static-debian12
COPY --from=build /flatserver /flatserver
ENTRYPOINT ["/flatserver"]
