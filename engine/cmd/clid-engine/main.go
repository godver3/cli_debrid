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
	"path/filepath"
	"runtime"
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
	Umask      uint32 `json:"umask"`
	LogLevel   string `json:"log_level"`
	// Read-only WebDAV share of the same tree ("" = off), e.g. "0.0.0.0:8284".
	WebDAVListen   string `json:"webdav_listen"`
	WebDAVUser     string `json:"webdav_user"`
	WebDAVPassword string `json:"webdav_password"`
}

// prepareMountPath: Linux mounts over an existing folder; WinFsp wants a
// free drive letter ("M:") or a folder that does not exist yet (its
// parent must).
func prepareMountPath(p string) error {
	if runtime.GOOS != "windows" {
		return os.MkdirAll(p, 0o755)
	}
	if len(p) == 2 && p[1] == ':' {
		return nil
	}
	if fi, err := os.Stat(p); err == nil {
		if !fi.IsDir() {
			return fmt.Errorf("mount path %s is a file", p)
		}
		if ents, _ := os.ReadDir(p); len(ents) > 0 {
			return fmt.Errorf("mount path %s must be empty", p)
		}
		_ = os.Remove(p) // an empty leftover folder from a previous run
	}
	return os.MkdirAll(filepath.Dir(p), 0o755)
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

	if cfg.WebDAVListen != "" {
		if cfg.WebDAVUser == "" || cfg.WebDAVPassword == "" {
			log.Warn("webdav not started: set webdav_user and webdav_password")
		} else if dl, err := net.Listen("tcp", cfg.WebDAVListen); err != nil {
			log.Error("webdav listen", "addr", cfg.WebDAVListen, "err", err)
		} else {
			dav := &http.Server{Handler: mount.WebDAVHandler(e, mount.WebDAVOptions{User: cfg.WebDAVUser, Password: cfg.WebDAVPassword}, log.With("component", "webdav")),
				ReadHeaderTimeout: 10 * time.Second}
			go func() {
				if err := dav.Serve(dl); err != nil && !errors.Is(err, http.ErrServerClosed) {
					log.Error("webdav stopped", "err", err)
				}
			}()
			defer dav.Close()
			log.Info("webdav listening", "addr", dl.Addr().String())
		}
	}

	var mounted mount.Mounted
	if !noMount {
		if cfg.MountPath == "" {
			return errors.New("mount_path is required (or run with --no-mount)")
		}
		// A dead mount left by a crashed engine makes the path unusable
		// (even mkdir fails on it): clear it first.
		mount.ClearStale(cfg.MountPath, log)
		if err := prepareMountPath(cfg.MountPath); err != nil {
			return err
		}
		m, err := mount.Start(e, cfg.MountPath, mount.Options{AllowOther: cfg.AllowOther, UID: cfg.UID, GID: cfg.GID, Umask: cfg.Umask}, log)
		if err != nil {
			return fmt.Errorf("mount %s: %w", cfg.MountPath, err)
		}
		log.Info("mounted", "path", cfg.MountPath)
		mounted = m
	}

	// Unmount first when told to stop: if the stop is cut short (a
	// container being killed), a live API matters far less than leaving
	// no dead mount behind.
	unmount := func() {
		if mounted == nil {
			return
		}
		if err := mounted.Unmount(); err != nil {
			log.Warn("unmount", "err", err)
		} else {
			log.Info("unmounted", "path", cfg.MountPath)
		}
		mounted = nil
	}
	defer unmount()
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	select {
	case <-sig:
	case <-e.ShutdownRequested():
	}
	log.Info("shutting down")
	unmount()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	return nil
}
