// Command rawnode runs a stock tsnet.Server without the connector package.
// The proof uses it as a control: the egress probe must see its default log
// upload, or a clean result for connector nodes would prove nothing.
package main

import (
	"flag"
	"log"

	"tailscale.com/tsnet"
)

func main() {
	dir := flag.String("dir", "", "state directory")
	control := flag.String("control", "", "control URL")
	flag.Parse()
	s := &tsnet.Server{Dir: *dir, ControlURL: *control, Hostname: "raw", Logf: log.Printf}
	if err := s.Start(); err != nil {
		log.Fatal(err)
	}
	select {}
}
