#!/usr/bin/env python

"""cat input | pawk [<options>] <expr>

A Python line-processor (like awk).

See https://github.com/alecthomas/pawk for details. Based on
http://code.activestate.com/recipes/437932/.
"""

import ast
import inspect
import os
import optparse
import re
import sys


RESULT_VAR_NAME = "__result"


# Store the last expression, if present, into variable var_name.
def save_last_expression(tree, var_name=RESULT_VAR_NAME):
    body = tree.body
    node = body[-1] if len(body) else None
    body.insert(0, ast.Assign(targets=[ast.Name(id=var_name, ctx=ast.Store())],
                              value=ast.Name(id="None", ctx=ast.Load())))
    if node and isinstance(node, ast.Expr):
        body[-1] = ast.copy_location(ast.Assign(
            targets=[ast.Name(id=var_name, ctx=ast.Store())], value=node.value), node)
    return ast.fix_missing_locations(tree)


def compile_command(text):
    tree = save_last_expression(compile(text, 'EXPR', 'exec', flags=ast.PyCF_ONLY_AST))
    return compile(tree, 'EXPR', 'exec')


def eval_in_context(codeobj, context, var_name=RESULT_VAR_NAME):
  exec codeobj in globals(), context
  return context.pop(var_name)


class Action(object):
    """Represents a single action to be applied to each line."""

    def __init__(self, pattern=None, cmd='l', have_end_statement=False, negate=False, strict=False):
        self.delim = None
        self.odelim = ' '
        self.negate = negate
        self.pattern = None if pattern is None else re.compile(pattern)
        self.cmd = cmd
        self.strict = strict
        self._compile(have_end_statement)

    @classmethod
    def from_options(cls, options, arg):
        negate, pattern, cmd = Action._parse_command(arg)
        return cls(pattern=pattern, cmd=cmd, have_end_statement=(options.end is not None), negate=negate, strict=options.strict)

    def _compile(self, have_end_statement):
        if not self.cmd:
            if have_end_statement:
                self.cmd = 't += line'
            else:
                self.cmd = 'l'
        self._codeobj = compile_command(self.cmd)

    def apply(self, context, line):
        """Apply action to line.

        :return: Line text or None.
        """
        match = self._match(line)
        if match is None:
            return None
        context['m'] = match
        try:
            return eval_in_context(self._codeobj, context)
        except:
            if not self.strict:
                return None
            raise

    def _match(self, line):
        if self.pattern is None:
            return self.negate
        match = self.pattern.search(line)
        if match is not None:
            return None if self.negate else match.groups()
        elif self.negate:
            return ()

    @staticmethod
    def _parse_command(arg):
        match = re.match(r'(?ms)(?:(!)?/((?:\\.|[^/])+)/)?(.*)', arg)
        negate, pattern, cmd = match.groups()
        cmd = cmd.strip()
        negate = bool(negate)
        return negate, pattern, cmd


class Context(dict):
    def apply(self, numz, line):
        l = line.rstrip()
        f = tuple([w for w in l.split(self.delim) if w])
        self.update(line=line, l=l, n=numz + 1, f=f, nf=len(f))

    @classmethod
    def from_options(cls, options, modules):
        self = cls()
        self['t'] = ''
        self['m'] = ()
        if options.imports:
            for imp in options.imports.split(','):
                m = __import__(imp.strip(), fromlist=['.'])
                self.update((k, v) for k, v in inspect.getmembers(m) if k[0] != '_')

        self.delim = options.delim.decode('string_escape') if options.delim else None
        self.odelim = options.delim_out.decode('string_escape')

        for m in modules:
            try:
                key = m.split('.')[0]
                self[key] = __import__(m)
            except:
                pass
        return self


def process(context, input, output, begin_statement, actions, end_statement, strict):
    """Process a stream."""
    # Override "print"
    old_stdout = sys.stdout
    sys.stdout = output
    write = output.write

    def write_result(result, when_true=None):
        if result is True:
            result = when_true
        elif isinstance(result, (list, tuple)):
            result = context.odelim.join(map(str, result))
        if result is not None and result is not False:
            result = str(result)
            write(result)
            if not result.endswith('\n'):
                write('\n')

    try:
        if begin_statement:
            write_result(eval_in_context(compile_command(begin_statement), context))

        for numz, line in enumerate(input):
            context.apply(numz, line)
            for action in actions:
                write_result(action.apply(context, line), when_true=line)

        if end_statement:
            write_result(eval_in_context(compile_command(end_statement), context))
    finally:
        sys.stdout = old_stdout


def parse_commandline(argv):
    parser = optparse.OptionParser()
    parser.set_usage(__doc__.strip())
    parser.add_option('-I', '--in_place', dest='in_place', help='modify given input file in-place', metavar='<filename>')
    parser.add_option('-i', '--import', dest='imports', help='comma-separated list of modules to "from x import *" from', metavar='<modules>')
    parser.add_option('-F', dest='delim', help='input delimiter', metavar='<delim>', default=None)
    parser.add_option('-O', dest='delim_out', help='output delimiter', metavar='<delim>', default=' ')
    parser.add_option('-B', '--begin', help='begin statement', metavar='<statement>')
    parser.add_option('-E', '--end', help='end statement', metavar='<statement>')
    parser.add_option('-s', '--statement', action='store_true', help='DEPRECATED. retained for backward compatibility')
    parser.add_option('--strict', action='store_true', help='abort on exceptions')
    return parser.parse_args(argv[1:])


# For integration tests.
def run(argv, input, output):
    options, args = parse_commandline(argv)

    if options.in_place:
        os.rename(options.in_place, options.in_place + '~')
        input = open(options.in_place + '~')
        output = open(options.in_place, 'w')

    # Auto-import. This is not smart.
    all_text = ' '.join([(options.begin or ''), ' '.join(args), (options.end or '')])
    modules = re.findall(r'([\w.]+)+(?=\.\w+)\b', all_text)

    context = Context.from_options(options, modules)
    actions = [Action.from_options(options, arg) for arg in args]
    if not actions:
        actions = [Action.from_options(options, '')]

    try:
        process(context, input, output, options.begin, actions, options.end, options.strict)
    finally:
        if options.in_place:
            output.close()
            input.close()


def main():
    try:
        run(sys.argv, sys.stdin, sys.stdout)
    except EnvironmentError as e:
        # Workaround for close failed in file object destructor: sys.excepthook is missing lost sys.stderr
        # http://stackoverflow.com/questions/7955138/addressing-sys-excepthook-error-in-bash-script
        print >> sys.stderr, str(e)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == '__main__':
    main()
