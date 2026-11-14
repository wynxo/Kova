import pytest
from kova.permissions import Approvals, Rule, evaluate_rules

@pytest.mark.parametrize('command', ['echo ok; rm x', 'echo ok && rm x', 'echo ok || rm x', 'echo ok | rm x', 'echo ok\nrm x', 'echo $(rm x)', 'echo `rm x`', 'echo $(echo $(rm x))'])
def test_compounds_never_inherit_first_allow(command):
    assert evaluate_rules([Rule('allow', 'run_command', 'echo*')], 'run_command', {'command': command}) != 'allow'


def test_precedence():
    rules = [Rule('allow', '*', '*'), Rule('ask', '*', '*'), Rule('deny', '*', '*', message='No')]
    assert evaluate_rules(rules, 'run_command', {'command': 'echo hi'}) == 'deny'

@pytest.mark.asyncio
async def test_exact_scope_and_persistence(tmp_path):
    approvals = Approvals(path=tmp_path / 'rules.json')
    args = {'command': 'echo hi'}
    event = approvals.create('c', 'run_command', args, cwd=tmp_path)
    approvals.answer('c', event['id'], 'allow_always')
    await approvals.wait(event['id'])
    fresh = Approvals(path=tmp_path / 'rules.json')
    assert fresh.policy('c', 'run_command', args, tmp_path) == ('allow', None)
    assert fresh.policy('c', 'run_command', {'command': 'echo hi; rm x'}, tmp_path)[0] is None
    assert fresh.policy('c', 'run_command', args, tmp_path / 'other')[0] is None

@pytest.mark.asyncio
async def test_session_exact_and_args_snapshot():
    a = Approvals()
    args = {'command': 'echo hi'}
    event = a.create('c', 'run_command', args)
    args['command'] = 'rm x'
    assert event['args']['command'] == 'echo hi'
    a.answer('c', event['id'], 'allow_session')
    await a.wait(event['id'])
    assert a.policy('c', 'run_command', {'command': 'echo hi'})[0] == 'allow'
    assert a.policy('other', 'run_command', {'command': 'echo hi'})[0] is None
    assert a.policy('c', 'run_command', args)[0] is None

@pytest.mark.parametrize('command', ['echo $(touch x)', 'echo `touch x`', 'echo ok > /etc/passwd', 'echo ${payload}', 'echo ok; sh -c bad', 'echo ok\\\nrm x'])
def test_ambiguous_shell_requires_exact_grant(command):
    assert evaluate_rules([Rule('allow', 'run_command', 'echo*')], 'run_command', {'command':command}) != 'allow'


def test_path_and_tool_matching():
    rules = [Rule('allow', 'read_*', 'src/*'), Rule('deny', '*', '*.key')]
    assert evaluate_rules(rules, 'read_file', {'path':'src/main.py'}) == 'allow'
    assert evaluate_rules(rules, 'write_file', {'path':'src/main.py'}) is None
    assert evaluate_rules(rules, 'read_file', {'path':'src/private.key'}) == 'deny'

@pytest.mark.asyncio
async def test_deny_reason_and_invalid_or_duplicate_answers():
    a=Approvals()
    a.rules=[Rule('deny', 'run_command', 'rm *', message='Use trash')]
    assert a.policy('c','run_command',{'command':'echo ok; rm x'}) == ('deny','Use trash')
    event=a.create('c','run_command',{'command':'pwd'})
    with pytest.raises(ValueError):
        a.answer('c', event['id'], 'invalid')
    a.answer('c',event['id'],'deny','Use read_file')
    with pytest.raises(KeyError):
        a.answer('c',event['id'],'allow')
    assert (await a.wait(event['id']))['message']=='Use read_file'

@pytest.mark.asyncio
async def test_session_grants_not_saved_and_edit_content_bound(tmp_path):
    a=Approvals(path=tmp_path/'rules.json')
    args={'path':'main.py','old_string':'a','new_string':'b'}
    event=a.create('c','edit_file',args)
    a.answer('c',event['id'],'allow_session')
    await a.wait(event['id'])
    assert a.policy('c','edit_file',args)[0]=='allow'
    assert a.policy('c','edit_file',{**args,'new_string':'evil'})[0] is None
    assert Approvals(path=a.path).rules==[]

@pytest.mark.asyncio
async def test_edit_card_has_change_summary():
    a=Approvals()
    event=a.create('c','edit_file',{'path':'main.py','old_string':'old\nline','new_string':'new'})
    assert event['change_summary']=='Replace 2 lines (8 chars) with 1 lines (3 chars)'
    a.answer('c',event['id'],'deny')
    await a.wait(event['id'])


@pytest.mark.parametrize('command', ["'rm' file", r"r\m file", 'echo ok; "rm" file'])
def test_denies_normalize_shell_quotes_and_escapes(command):
    assert evaluate_rules([Rule('deny','run_command','rm *')], 'run_command', {'command':command}) == 'deny'


@pytest.mark.parametrize('decision', ['allow_session', 'allow_always'])
@pytest.mark.parametrize('tool', ['run_command', 'job_add'])
async def test_destructive_approval_is_one_shot(tmp_path, decision, tool):
    a = Approvals(path=tmp_path / 'rules.json')
    args = {'command': 'rm -rf build'}
    event = a.create('c', tool, args, cwd=tmp_path)
    a.answer('c', event['id'], decision)
    assert (await a.wait(event['id']))['decision'] == decision
    assert a.rules == []
    assert a.policy('c', tool, args, tmp_path)[0] is None
    assert Approvals(path=a.path).rules == []


def test_existing_allow_cannot_bypass_destructive_approval(tmp_path):
    from kova.permissions import action_key

    args = {'command': 'rm -rf build'}
    a = Approvals(path=tmp_path / 'rules.json')
    a.rules = [Rule('allow', 'run_command', exact=action_key('run_command', args, tmp_path))]
    a.save()
    fresh = Approvals(path=a.path)
    assert fresh.policy('c', 'run_command', args, tmp_path)[0] != 'allow'
    fresh.rules.append(Rule('deny', 'run_command', '*', message='No deletion'))
    assert fresh.policy('c', 'run_command', args, tmp_path) == ('deny', 'No deletion')
