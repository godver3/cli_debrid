// Command clid-engine is cli_debrid's optional built-in mount: it imports
// NZBs, serves their files over FUSE and exposes a localhost API.
//
//	clid-engine --config /config/engine.json
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/api"
	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/mount"
)

// fileConfig is engine.Config plus mount and logging options.
type fileConfig struct {
	engine.Config
	AllowOther bool   `json:"allow_other"`
	UID        uint32 `json:"uid"`
	GID        uint32 `json:"gid"`
	LogLevel   string `json:"log_level"`
}

func main() {
	cfgPath := flag.String("config", "engine.json", "path to the JSON config")
	noMount := flag.Bool("no-mount", false, "run the API without mounting (testing)")
	flag.Parse()
	if err := run(*cfgPath, *noMount); err != nil {
		fmt.Fprintln(os.Stderr, "clid-engine:", err)
		os.Exit(1)
	}
}

func run(cfgPath string, noMount bool) error {
	raw, err := os.ReadFile(cfgPath)
	if err != nil {
		return err
	}
	var cfg fileConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return fmt.Errorf("config %s: %w", cfgPath, err)
	}
	level := slog.LevelInfo
	_ = level.UnmarshalText([]byte(cfg.LogLevel))
	log := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level}))

	e, err := engine.New(cfg.Config, log)
	if err != nil {
		return err
	}
	defer e.Close()

	ln, err := net.Listen("tcp", e.Config().Listen)
	if err != nil {
		return fmt.Errorf("api listen: %w", err)
	}
	srv := &http.Server{Handler: api.Handler(e), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("api stopped", "err", err)
		}
	}()
	log.Info("api listening", "addr", ln.Addr().String())

	if !noMount {
		if cfg.MountPath == "" {
			return errors.New("mount_path is required (or run with --no-mount)")
		}
		if err := os.MkdirAll(cfg.MountPath, 0o755); err != nil {
			return err
		}
		fsrv, err := mount.Mount(e, cfg.MountPath, mount.Options{AllowOther: cfg.AllowOther, UID: cfg.UID, GID: cfg.GID}, log)
		if err != nil {
			return fmt.Errorf("mount %s: %w", cfg.MountPath, err)
		}
		log.Info("mounted", "path", cfg.MountPath)
		defer func() {
			if err := fsrv.Unmount(); err != nil {
				log.Warn("unmount", "err", err)
			}
		}()
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Info("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	return nil
}
