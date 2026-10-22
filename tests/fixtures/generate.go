// Run from the Skiva repository: go run /tmp/skiva-sdk-canonical.go
package main
import (
 "encoding/json"
 "os"
 "skiva/packages/protocol"
)
func main() {
 files := []protocol.File{
  {Path:"中文/é<&>.md",Type:"file",Size:0,Digest:protocol.Digest(nil),Mode:0444},
  {Path:"a/line\u2028separator\u2029.md",Type:"file",Size:0,Digest:protocol.Digest(nil),Mode:0555},
  {Path:"a/quote\"tab\t.md",Type:"file",Size:0,Digest:protocol.Digest(nil),Mode:0444},
 }
 b,err:=protocol.Canonical(files); if err!=nil {panic(err)}
 json.NewEncoder(os.Stdout).Encode(map[string]any{"canonical":string(b),"manifest":protocol.Manifest{SchemaVersion:1,SnapshotID:"go-fixture",ProfileID:"profile",Generation:1,Files:files,ManifestDigest:protocol.Digest(b)}})
}
