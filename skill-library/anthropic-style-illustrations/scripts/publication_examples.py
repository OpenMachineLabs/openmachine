#!/usr/bin/env python3
"""Re-render original publication examples from retained briefs and reviewed cases.

The official source-context receipt records the executed transport and any gap.
Rendering does not refetch source text or turn a fallback into SafeFetcher acceptance.
"""
import json
from pathlib import Path
from asi.library import ROOT,rows,digest,write_json,search
from asi.planning import plan,sections
from asi.rendering import render


def main():
    cases={r['case_id']:r for r in rows(ROOT/'knowledge/analyses.jsonl')}
    articles={r['id']:r for r in rows(ROOT/'knowledge/articles.jsonl')}
    def reference(case,transfer):
        r=cases[case]
        return {'figure_id':r['figure_id'],'article_id':r['article_id'],'source_hash':r['source_hash'],
                'source_url':articles[r['article_id']]['url'],'evidence_level':'validated_analysis','analysis':r,'transfer':transfer}
    def base(text,title,family,palette,refs):
        return {'schema_version':1,'input_hash':digest(text),'title':title,'family':family,'palette':palette,
                'subtitle':'Original explanatory design • No performance measurements',
                'references':[reference(c,t) for c,t in refs], 'semantic_review':{'reviewer':'Codex author self-review','date':'2026-10-20','rationale':'Checked claims, distinctions and limitations against the complete available input; no PNG visual review.'}}
    target=ROOT/'examples/full-publication'
    text=(target/'brief.md').read_text(); part=sections(text)[2]
    claim=part['text'].split('\n',1)[1].strip()
    spec=base(text,'Three exchanges, three contracts','comparison','engineering',[
        ('A3','Transfer explicit selection; replace the branch gallery with an aligned contract comparison.'),
        ('A4','Transfer the named join and independent inputs; never interpret peer card size as equal effort.'),
        ('A6','Transfer distinct artifact and critique messages plus the accepted exit; do not imply convergence.')])
    spec.update(claims=[{'id':'contract','section_id':part['id'],'quote':claim,'text':'Selection, combination and revision exchange different information.'}],
        columns=['Choose','Combine','Revise'],
        items=[{'id':'send','cells':['Request → specialist','Independent contributions → join','Draft → evaluator'],'claim_ids':['contract']},
               {'id':'return','cells':['One selected response','One combined result','Critique → revision; accepted result exits'],'claim_ids':['contract']}],
        caption='Read the branch contract before choosing a pattern. This comparison implies neither performance ordering nor guaranteed improvement.',
        alt='Three aligned columns distinguish selecting one handler, combining contributions and returning critique before acceptance.')
    render(spec,text,ROOT/'knowledge',target)
    write_json(target/'retrieval.json',search(ROOT/'knowledge','routing fork feedback aggregation',reviewed_only=True,limit=6))
    write_json(target/'brief-plan.json',plan(text,budget=1,section=part['id']))
    (target/'design.md').write_text('''# Original design brief

Reader question: What does a branch or return actually promise?

Use three aligned columns and two message rows. Position encodes corresponding
questions; arrows denote information exchange. Green accents group the comparison,
not correctness or quantitative performance. Text labels carry all distinctions.
Transfer A3/A4/A6's different contracts, not their geometry. No source artwork,
model metrics or provider branding is reused. The source-context coverage receipt
records every editorial section; the editable specification binds the original
semantic brief to the figure. Captions and alt text are in figure.json.

Keep frameworks, the capability inventory and application anecdotes in prose.
The scope warning remains visible. Review labels at 1200 px and half-size before
publication. See references/visual-review.md for the separate AI PNG review; geometry QA is not approval.
''')
    target=ROOT/'examples/archive-pilot';text=(target/'article.md').read_text();parts=sections(text)
    claims=[{'id':'notes','section_id':parts[3]['id'],'quote':parts[3]['text'].split('\n',1)[1].strip(),'text':'Two independent notes inform one editor under the same identifier.'},
            {'id':'revision','section_id':parts[4]['id'],'quote':parts[4]['text'].split('\n',1)[1].strip(),'text':'Specific unresolved questions return to intake; acceptance is not guaranteed.'}]
    spec=base(text,'Two notes, one accountable decision','flow','scientific',[
        ('A4','Transfer independent branches and an explicit join to distinct description and permissions reviews.'),
        ('A6','Transfer a specific return message; the editor may defer rather than converge.'),
        ('P2','Carry the same case identifier across work and judgment; tool menus are not stages.')])
    spec.update(claims=claims,nodes=[{'id':i,'label':label,'row':row,'column':col,'claim_ids':['notes']} for i,label,row,col in [
        ('intake','Intake • Object ID',0,0),('description','Description • Identity note',0,1),('permissions','Permissions • Display basis',1,1),('editor','Editor • Reconcile both notes',0,2)]],
        edges=[{'from':a,'to':b,'meaning':m,'claim_ids':[c]} for a,b,m,c in [
            ('intake','description','same object ID and description','notes'),('intake','permissions','same object ID and description','notes'),
            ('description','editor','identity note','notes'),('permissions','editor','permissions note','notes'),('editor','intake','specific unresolved question','revision')]],
        caption='Proposed workflow. Arrows carry requests or notes, not elapsed time. The editor may approve with stated uncertainty, restrict the record or defer; repetition does not guarantee approval.',
        alt='Intake sends the same identifier to two independent reviewers. Both notes reach an editor. A separate outside lane returns a specific unresolved question to intake.')
    render(spec,text,ROOT/'knowledge',target)
    reviewed=plan(text,budget=2)
    for d in reviewed['decisions']:
        d.update(decision='no_image',family=None,evidence=[],reason='Prose states a straightforward boundary; another diagram would repeat the central explanation.')
        if d['section_id']==parts[3]['id']:
            d.update(decision='illustrate',family='flow',evidence=['The notes can be prepared independently'],reason='Readers must see that both notes are required before reconciliation.')
        if d['section_id']==parts[5]['id']:
            d.update(decision='illustrate',family='comparison',evidence=['An error message should identify the failed action'],reason='Matched messages make omitted recovery information inspectable.')
    reviewed['review']={'reviewer':'Codex author self-review','date':'2026-10-20','rationale':'Read all eight sections; allocate figures to handoff dependency and recoverable interface detail, keep population caveat in prose.'}
    write_json(target/'plan.json',plan(text,budget=2,reviewed=reviewed))
    write_json(target/'retrieval.json',search(ROOT/'knowledge','fork parallel independent work aggregation',reviewed_only=True,limit=3))
    part=parts[5];claim=part['text'].split('\n',1)[1].strip()
    detail=base(text,'Make an unfinished record recoverable','comparison','scientific',[
        ('E5','Transfer semantic selection instead of smaller type; retain actionable information.'),
        ('E6','Transfer recovery guidance; useful explanations may be longer.'),
        ('E8','Negative contrast only: remove neither the failed action nor the recovery step.')])
    detail.update(claims=[{'id':'message','section_id':part['id'],'quote':claim,'text':'Show a failed action and useful next step without hiding uncertainty.'}],
        columns=['Missing recovery information','Original proposed response'],
        items=[{'id':'message','cells':['Unable to publish.','Record PHOTO-014 remains restricted. The display basis is unresolved. Ask the permissions reviewer to complete the note.'],'claim_ids':['message']}],
        subtitle='Matched state • Illustrative record identifier',caption='Both messages describe the same hypothetical incomplete record. The longer response names the state, missing evidence and next action. It does not invent permission.',
        alt='Matched messages contrast an unexplained publication failure with a proposed message preserving the record ID, restricted status, unresolved display basis and next reviewer action.')
    render(detail,text,ROOT/'knowledge',target/'message-detail')
    (target/'design.md').write_text('''# Original cross-domain design brief

The complete article is an original proposed archive pilot. It reports no completed
pilot, measured quantities or permissions outcome. The main figure answers who
must provide which information before an editor decides. Card positions encode
responsibility, and the outside return lane carries an unresolved question. Stable
identifiers transfer P2's traceability; the join transfers A4; the critique transfers
A6 without promising convergence. Neutral white panels and gray-blue accents suit
a procedural explanation without brand imitation.

The companion figure transfers E5/E6's usable information and uses E8 only as a
negative contrast. Both messages concern the same invented record and state.
PHOTO-014 is a schematic identifier, not personal or collection data. No actual
console screenshot is copied. The original article's population caveat uses F4's
lesson about denominators in prose; without observations, a quantitative figure
would be misleading. Titles, captions, alt text and claim links remain editable.

Semantic self-review is recorded separately from geometry QA. See references/visual-review.md
for the separate AI inspection of both rendered PNGs.
''')
    print(json.dumps({'outputs':['examples/full-publication/figure.png','examples/archive-pilot/figure.png','examples/archive-pilot/message-detail/figure.png'],'visual_review':'NOT RUN'},indent=2))


if __name__=='__main__':main()
