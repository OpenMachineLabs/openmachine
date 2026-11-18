package daemon

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// ownEntry gives the daemon t starts next, in this process or a child, a free
// entry port of its own, so om's 11400 and its pages' ports stay om's.
func ownEntry(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := strconv.Itoa(l.Addr().(*net.TCPAddr).Port)
	l.Close()
	t.Setenv("OM_LOCAL_PORT", port)
	return port
}

// runNative runs a test's daemon, refusing one with om's own ports.
func runNative(ctx context.Context, core daemon.Core) error {
	if os.Getenv("OM_LOCAL_PORT") == "" {
		panic("a test daemon without OM_LOCAL_PORT takes om's ports 11400-11699; call ownEntry first")
	}
	return daemon.Run(ctx, core)
}

// TestDaemonPorts: every daemon here starts through runNative.
func TestDaemonPorts(t *testing.T) {
	files, _ := filepath.Glob("*.go")
	fset := token.NewFileSet()
	for _, name := range files {
		f, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		pkg := "" // the name this file gives the native package
		for _, imp := range f.Imports {
			if imp.Path.Value == strconv.Quote("github.com/OpenMachineLabs/openmachine/daemon") {
				pkg = "native"
				if imp.Name != nil {
					pkg = imp.Name.Name
				}
			}
		}
		for _, decl := range f.Decls {
			if fn, ok := decl.(*ast.FuncDecl); ok && fn.Name.Name == "runNative" && fn.Recv == nil {
				continue
			}
			ast.Inspect(decl, func(n ast.Node) bool {
				if s, ok := n.(*ast.SelectorExpr); ok && s.Sel.Name == "Run" {
					if x, ok := s.X.(*ast.Ident); ok && x.Name == pkg {
						t.Errorf("%s: daemon.Run outside runNative may take om's ports", fset.Position(s.Pos()))
					}
				}
				return true
			})
		}
	}
	if len(files) == 0 {
		t.Fatal("no Go files")
	}
}
