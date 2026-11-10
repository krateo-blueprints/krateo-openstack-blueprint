// Command umbrella-crd-size fails when the umbrella's generated Openstack CRD could outgrow etcd.
//
// WHY. core-provider turns the umbrella's values.schema.json into the Openstack CRD, and a CRD
// stores EVERY version it still serves. Components that stay pinned at an origin version keep
// several served at once (two were observed together on a real cluster). #209's first fix embedded
// all 36 widened component schemas in componentValues, which made this CRD 203,608 bytes per served
// version as JSON (396 KB as crdgen's YAML). The umbrella now passes componentValues through as
// open objects and lets each component's own CRD do the typing, and this gate keeps it that way.
//
// WHAT IT MEASURES. The CRD exactly as core-provider 2.13.72 generates it: the same generator
// (plumbing v1.28.1 crdgen.Generate, pinned in go.mod) fed the same status schema
// (status.schema.json, copied unchanged from core-provider 2.13.72
// go/core-provider/internal/tools/crd/generation/statics/status.schema.json). The size is the
// object's JSON encoding, the form the apiserver stores.
//
// TWO BOUNDS. Both must hold.
//
//  1. Per served version, at most PerVersionCap = 128 KiB (131,072 bytes). This is the DESIGN
//     bound, and it is the one that catches a design change: embedding the component schemas
//     again (203,608 bytes) fails it outright. A generous margin over today's 32,640 bytes and
//     main's 52,669 still leaves room for the umbrella's own values to grow. Diego chose it.
//
//  2. Per served version x ServedVersions <= Limit, with ServedVersions = 4 and Limit = 1 MiB.
//     This is the ETCD-LEVEL bound: the whole CRD object, every served version together, must stay
//     clear of etcd's default 1.5 MiB request size. Four is an ASSUMPTION, not a measurement: two
//     served versions were observed together, and four leaves room for an install whose
//     components stay pinned across a few releases. On its own this bound is too loose to catch a
//     design change: the embedded design passes it (814,432 bytes), which is why bound 1 exists.
//     Bound 1 implies bound 2 today (4 x 128 KiB = 512 KiB); bound 2 is kept so that raising the
//     cap, or a CRD that serves more versions, still has to answer to etcd.
//
// It fails, never passes, when the schema is missing or empty or the CRD does not generate.
//
// Usage: go run . [path/to/umbrella/values.schema.json]
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	_ "embed"

	"github.com/krateo-platformops/plumbing/crdgen"
	"sigs.k8s.io/yaml"
)

const (
	perVersionCap  = 128 << 10 // 131,072 bytes: bound 1, the design bound.
	servedVersions = 4         // ASSUMPTION; see the package comment.
	limitBytes     = 1 << 20   // 1 MiB: bound 2, the etcd-level bound.
)

//go:embed status.schema.json
var statusSchema []byte

func fail(format string, a ...interface{}) {
	fmt.Printf("FAIL: "+format+"\n", a...)
	os.Exit(1)
}

func main() {
	path := filepath.Join("..", "..", "blueprints", "openstack", "chart", "values.schema.json")
	if len(os.Args) > 1 {
		path = os.Args[1]
	}
	spec, err := os.ReadFile(path)
	if err != nil {
		fail("cannot read the umbrella schema at %s: %v -- nothing was measured", path, err)
	}
	if len(spec) == 0 {
		fail("the umbrella schema at %s is empty -- nothing was measured", path)
	}
	manifest, err := crdgen.Generate(crdgen.Options{
		Group:        "composition.krateo.io",
		Version:      "v0-0-0-gate",
		Kind:         "Openstack",
		Managed:      true,
		Categories:   []string{"compositions", "comps"},
		SpecSchema:   spec,
		StatusSchema: statusSchema,
	})
	if err != nil {
		fail("crdgen could not generate the Openstack CRD: %v", err)
	}
	asJSON, err := yaml.YAMLToJSON(manifest)
	if err != nil {
		fail("the generated CRD is not valid YAML: %v", err)
	}
	var probe map[string]interface{}
	if err := json.Unmarshal(asJSON, &probe); err != nil || probe["kind"] != "CustomResourceDefinition" {
		fail("crdgen did not produce a CustomResourceDefinition -- nothing was measured")
	}
	size := len(asJSON)
	total := size * servedVersions
	fmt.Printf("umbrella Openstack CRD: %d bytes (JSON) per served version; cap %d\n", size, perVersionCap)
	fmt.Printf("x %d served versions (assumed) = %d bytes; limit %d\n", servedVersions, total, limitBytes)
	failed := false
	if size > perVersionCap {
		fmt.Printf("FAIL: bound 1: one served version is %d bytes > %d. Keep componentValues open in the "+
			"umbrella (hack/gen-componentvalues-schema.py) and let each component CRD type its overrides (#209).\n",
			size, perVersionCap)
		failed = true
	}
	if total > limitBytes {
		fmt.Printf("FAIL: bound 2: %d bytes x %d served versions = %d > %d, too close to etcd's request limit.\n",
			size, servedVersions, total, limitBytes)
		failed = true
	}
	if failed {
		os.Exit(1)
	}
	fmt.Println("OK")
}
