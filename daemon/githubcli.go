package daemon

import (
	"bufio"
	"bytes"
	"cmp"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// GitCredential is om git-credential, a session's only Git credential helper for its broker
// (gitcredentials(7)). get exchanges the session's capability for one command's credential for
// the remote's repository; store and erase do nothing, so the credential
// lives only in git's memory. A refusal stops git from asking anyone else.
func GitCredential(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) != 1 {
		fmt.Fprintln(stderr, "usage: om git-credential get|store|erase")
		return 2
	}
	request := map[string]string{}
	scanner := bufio.NewScanner(io.LimitReader(stdin, 64<<10))
	for scanner.Scan() && scanner.Text() != "" {
		if key, value, ok := strings.Cut(scanner.Text(), "="); ok {
			request[key] = value
		}
	}
	broker := os.Getenv("OM_BROKER")
	if args[0] != "get" || broker == "" || request["protocol"]+"://"+request["host"] != broker {
		return 0
	}
	quit := func(message string) int {
		fmt.Fprintf(stderr, "om: %s\n", message)
		fmt.Fprintln(stdout, "quit=1")
		return 0
	}
	var reply struct{ Username, Password, Message string }
	status, err := brokerCall(broker, "/v1/credential", map[string]string{"path": request["path"]}, &reply)
	switch {
	case err != nil:
		return quit("the Open Machine Code service did not answer: " + err.Error())
	case status != http.StatusOK || reply.Password == "":
		return quit(cmp.Or(reply.Message, "Open Machine refused this Git command"))
	}
	fmt.Fprintf(stdout, "username=%s\npassword=%s\n", reply.Username, reply.Password)
	return 0
}

// brokerCall posts one JSON request to the session's broker with its capability and decodes the
// JSON reply, whatever its status. It reaches only the server holding the key the session's routing
// pins, as Git does. A Full Access session also sends its proof when the broker asks: the file named
// after its capability in the directory its own routing names, which only unsandboxed work can
// read. Nothing in a reply chooses what is read or sent.
func brokerCall(broker, path string, body, out any) (int, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return 0, err
	}
	client := PinnedClient(routed("http."+broker+".pinnedPubkey"), 90*time.Second)
	capability := os.Getenv("OM_BROKER_CAPABILITY")
	send := func(proof string) (int, []byte, error) {
		req, err := http.NewRequest(http.MethodPost, broker+path, bytes.NewReader(raw))
		if err != nil {
			return 0, nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+capability)
		if proof != "" {
			req.Header.Set("X-OM-Broker-Proof", proof)
		}
		res, err := client.Do(req)
		if err != nil {
			return 0, nil, err
		}
		defer res.Body.Close()
		reply, err := io.ReadAll(io.LimitReader(res.Body, 4<<20))
		return res.StatusCode, reply, err
	}
	status, reply, err := send("")
	var need struct {
		Error string `json:"error"`
	}
	if err == nil && status == http.StatusUnauthorized && json.Unmarshal(reply, &need) == nil && need.Error == "code_proof_required" {
		if proof, ok := readProof(routed("credential."+broker+"."+ProofsKey), capability); ok {
			status, reply, err = send(proof)
		}
	}
	if err != nil {
		return 0, err
	}
	return status, json.Unmarshal(reply, out)
}

// routed returns the value of key in the session's process-scoped Git configuration, as the daemon
// set it.
func routed(key string) string {
	n, _ := strconv.Atoi(os.Getenv("GIT_CONFIG_COUNT"))
	for i := range min(n, 16) {
		if os.Getenv(fmt.Sprintf("GIT_CONFIG_KEY_%d", i)) == key {
			return os.Getenv(fmt.Sprintf("GIT_CONFIG_VALUE_%d", i))
		}
	}
	return ""
}

var proofValue = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)

// readProof reads the proof file named after capability in dir, refusing a link or anything but a
// small regular file.
func readProof(dir, capability string) (string, bool) {
	if !filepath.IsAbs(dir) || !proofValue.MatchString(capability) {
		return "", false
	}
	f, err := os.OpenFile(filepath.Join(dir, capability), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return "", false
	}
	defer f.Close()
	if fi, err := f.Stat(); err != nil || !fi.Mode().IsRegular() || fi.Size() > 64 {
		return "", false
	}
	b, err := io.ReadAll(io.LimitReader(f, 65))
	proof := strings.TrimSpace(string(b))
	return proof, err == nil && proofValue.MatchString(proof)
}

const githubUsage = `usage:
  om github pr create --title TITLE [--body TEXT | --body-file FILE] [--base BRANCH] [--head BRANCH] [--draft]
  om github pr view [NUMBER]
  om github pr list [--state open|closed|all] [--head BRANCH]
  om github ci [REF]
Each takes --remote NAME (default origin) or --repo OWNER/NAME, and --json for the raw result.
They use this computer's Open Machine GitHub connection through the SSH session; nothing else is called.
`

// GitHub is om github: a session's fixed pull request and CI operations,
// through its broker and the account's GitHub connection in Cloud. There is no merge,
// workflow dispatch, release or other call, and no gh or token.
func GitHub(args []string, stdout, stderr io.Writer) int {
	if os.Getenv("OM_BROKER") == "" {
		fmt.Fprintln(stderr, "om: run om github inside an Open Machine SSH session")
		return 1
	}
	words := strings.Join(args[:min(2, len(args))], " ")
	fs := flag.NewFlagSet("om github", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	remote := fs.String("remote", "origin", "")
	repo := fs.String("repo", "", "")
	raw := fs.Bool("json", false, "")
	title, body, bodyFile := fs.String("title", "", ""), fs.String("body", "", ""), fs.String("body-file", "", "")
	base, head, state := fs.String("base", "", ""), fs.String("head", "", ""), fs.String("state", "open", "")
	draft := fs.Bool("draft", false, "")
	skip := 2
	if words == "ci" || strings.HasPrefix(words, "ci ") {
		skip = 1
	}
	if len(args) < skip {
		fmt.Fprint(stderr, githubUsage)
		return 2
	}
	var positional []string
	for rest := args[skip:]; ; {
		if fs.Parse(rest) != nil {
			fmt.Fprint(stderr, githubUsage)
			return 2
		}
		if fs.NArg() == 0 {
			break
		}
		positional, rest = append(positional, fs.Arg(0)), fs.Args()[1:]
	}
	fail := func(message string) int {
		fmt.Fprintf(stderr, "om: %s\n", message)
		return 1
	}
	repository := *repo
	if repository == "" {
		url, _ := gitOutput("config", "--get", "remote."+*remote+".url")
		m := githubRemote.FindStringSubmatch(url)
		if m == nil {
			return fail(fmt.Sprintf("remote %s is not a github.com repository; use --repo OWNER/NAME", *remote))
		}
		repository = m[1] + "/" + m[2]
	}
	branch := func() string {
		b, _ := gitOutput("symbolic-ref", "--quiet", "--short", "HEAD")
		return b
	}
	optional := func(value string) any {
		if value == "" {
			return nil
		}
		return value
	}
	var call map[string]any
	switch {
	case words == "pr create" && len(positional) == 0:
		text := *body
		if *bodyFile != "" {
			b, err := os.ReadFile(*bodyFile)
			if err != nil {
				return fail(err.Error())
			}
			text = string(b)
		}
		if *head == "" {
			*head = branch()
		}
		if *title == "" || *head == "" {
			return fail("pr create needs --title, and --head unless a branch is checked out")
		}
		call = map[string]any{"operation": "pull_create", "title": *title, "body": text, "head": *head, "base": optional(*base), "draft": *draft}
	case words == "pr view" && len(positional) <= 1:
		number := 0
		if len(positional) == 1 {
			n, err := strconv.Atoi(positional[0])
			if err != nil || n < 1 {
				return fail("pr view takes a pull request number")
			}
			number = n
		} else if number = openPull(repository, branch()); number == 0 {
			return fail("no open pull request has the current branch as its head; give its number")
		}
		call = map[string]any{"operation": "pull_view", "number": number}
	case words == "pr list" && len(positional) == 0:
		call = map[string]any{"operation": "pull_list", "state": *state, "head": optional(*head), "base": nil}
	case skip == 1 && len(positional) <= 1:
		ref := "HEAD"
		if len(positional) == 1 {
			ref = positional[0]
		}
		sha, err := gitOutput("rev-parse", "--verify", "--quiet", ref+"^{commit}")
		if err != nil || sha == "" {
			sha = ref // a branch on GitHub not checked out here
		}
		call = map[string]any{"operation": "checks", "ref": sha}
	default:
		fmt.Fprint(stderr, githubUsage)
		return 2
	}
	call["v"] = 1
	var reply struct {
		Result         json.RawMessage `json:"result"`
		Message, Error string
		Effect         string `json:"effect"`
	}
	status, err := brokerCall(os.Getenv("OM_BROKER"), "/v1/github", map[string]any{"repository": repository, "call": call}, &reply)
	if err != nil || status != http.StatusOK {
		message := cmp.Or(reply.Message, "the Open Machine Code service did not answer")
		if err != nil {
			message += ": " + err.Error()
		}
		// A lost or late answer leaves a creation unknown, like the broker's own effect_unknown.
		if call["operation"] == "pull_create" && (err != nil || reply.Effect == "effect_unknown") {
			message += fmt.Sprintf("; the pull request may exist: check with om github pr list --head %s before creating it again", *head)
		}
		return fail(message)
	}
	if *raw {
		stdout.Write(append(reply.Result, '\n'))
		return 0
	}
	printResult(stdout, call["operation"].(string), reply.Result)
	return 0
}

func gitOutput(args ...string) (string, error) {
	out, err := exec.Command("git", args...).Output()
	return strings.TrimSpace(string(out)), err
}

// openPull returns the number of the one open pull request whose head is branch, or 0.
func openPull(repository, branch string) int {
	if branch == "" {
		return 0
	}
	var reply struct {
		Result []struct{ Number int } `json:"result"`
	}
	status, err := brokerCall(os.Getenv("OM_BROKER"), "/v1/github", map[string]any{"repository": repository,
		"call": map[string]any{"v": 1, "operation": "pull_list", "state": "open", "head": branch, "base": nil}}, &reply)
	if err != nil || status != http.StatusOK || len(reply.Result) != 1 {
		return 0
	}
	return reply.Result[0].Number
}

type pullResult struct {
	Number         int
	URL, Title     string
	State          string
	Draft, Merged  bool
	Mergeable      *bool
	MergeableState string `json:"mergeable_state"`
	Head, Base     struct{ Ref, Repository string }
	Author, Body   string
	UpdatedAt      string `json:"updated_at"`
}

func (p pullResult) line() string {
	state := p.State
	if p.Draft {
		state += " (draft)"
	}
	if p.Merged {
		state = "merged"
	}
	return fmt.Sprintf("#%d %s: %s", p.Number, state, p.Title)
}

func printResult(w io.Writer, operation string, raw json.RawMessage) {
	switch operation {
	case "pull_create":
		var p pullResult
		json.Unmarshal(raw, &p)
		fmt.Fprintf(w, "%s\n%s\n", p.URL, p.line())
	case "pull_view":
		var p pullResult
		json.Unmarshal(raw, &p)
		fmt.Fprintf(w, "%s\n%s -> %s, mergeable: %s, by %s, updated %s\n%s\n", p.line(), p.Head.Ref, p.Base.Ref,
			cmp.Or(p.MergeableState, "unknown"), p.Author, p.UpdatedAt, p.URL)
		if p.Body != "" {
			fmt.Fprintf(w, "\n%s\n", p.Body)
		}
	case "pull_list":
		var list []pullResult
		json.Unmarshal(raw, &list)
		for _, p := range list {
			fmt.Fprintf(w, "%s (%s -> %s) %s\n", p.line(), p.Head.Ref, p.Base.Ref, p.URL)
		}
		if len(list) == 0 {
			fmt.Fprintln(w, "no pull requests")
		}
	case "checks":
		var c struct {
			SHA, State string
			CheckRuns  []struct{ Name, Status, Conclusion, URL string } `json:"check_runs"`
			Statuses   []struct{ Context, State, Description, URL string }
		}
		json.Unmarshal(raw, &c)
		fmt.Fprintf(w, "%s statuses: %s\n", c.SHA, cmp.Or(c.State, "none"))
		for _, r := range c.CheckRuns {
			fmt.Fprintf(w, "check %s: %s %s %s\n", r.Name, r.Status, r.Conclusion, r.URL)
		}
		for _, s := range c.Statuses {
			fmt.Fprintf(w, "status %s: %s %s %s\n", s.Context, s.State, s.Description, s.URL)
		}
		if len(c.CheckRuns)+len(c.Statuses) == 0 {
			fmt.Fprintln(w, "no checks or statuses yet")
		}
	}
}
