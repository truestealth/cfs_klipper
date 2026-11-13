"""Small offline host for an externally supplied Klipper gcode_macro module."""
import configparser
import importlib.util
import shlex


class Config:
    error = RuntimeError

    def __init__(self, printer, name, values):
        self.printer, self.name, self.values = printer, name, values

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, name, default=None):
        return self.values.get(name, default)

    def get_prefix_options(self, prefix):
        return [key for key in self.values if key.startswith(prefix)]


class Command:
    error = RuntimeError

    def __init__(self, raw):
        self.raw = raw.strip()
        self.params = dict(part.split('=', 1) for part in shlex.split(self.raw)
                           if '=' in part)

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name, default)
        try:
            value = int(value)
        except (TypeError, ValueError) as error:
            raise self.error('invalid ' + name) from error
        if (minval is not None and value < minval) or (maxval is not None and value > maxval):
            raise self.error('out of range ' + name)
        return value

    def get_command_parameters(self):
        return dict(self.params)

    def get_raw_command_parameters(self):
        return self.raw


class GCode:
    error = RuntimeError

    def __init__(self):
        self.commands, self.mux, self.lines = {}, {}, []

    def is_traditional_gcode(self, name):
        return len(name) > 1 and name[0].upper() in 'GMT' and name[1:].isdigit()

    def register_command(self, name, handler, desc=None):
        name = name.upper()
        previous = self.commands.pop(name, None)
        if handler is not None:
            if previous is not None:
                self.commands[name] = previous
                raise RuntimeError('duplicate command ' + name)
            self.commands[name] = handler
        return previous

    def register_mux_command(self, command, key, value, handler, desc=None):
        self.mux[(command, value)] = handler

    def run_script_from_command(self, script):
        for line in script.splitlines():
            line = line.strip()
            if not line or line.startswith(('#', ';')):
                continue
            name, _, raw = line.partition(' ')
            name = name.upper()
            self.lines.append((name, raw.strip()))
            if name == 'SET_GCODE_VARIABLE':
                command = Command(raw)
                self.mux[(name, command.get('MACRO'))](command)
            elif name in self.commands:
                self.commands[name](Command(raw))

    def respond_info(self, message):
        pass

    def _respond_error(self, message):
        raise RuntimeError(message)


class Host:
    config_error = command_error = RuntimeError

    def __init__(self, module):
        self.gcode = GCode()
        self.objects = {'gcode': self.gcode}
        self.events = []
        self.objects['gcode_macro'] = module.PrinterGCodeMacro(Config(self, 'gcode_macro', {}))
        self.module = module

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def lookup_objects(self):
        return list(self.objects.items())

    def load_object(self, config, name):
        return self.objects[name]

    def register_event_handler(self, name, callback):
        self.events.append((name, callback))

    def get_reactor(self):
        return self

    def monotonic(self):
        return 0.

    def macro(self, name, values):
        section = 'gcode_macro ' + name
        obj = self.module.GCodeMacro(Config(self, section, values))
        self.objects[section] = obj
        return obj

    def connect(self):
        for name, callback in self.events:
            if name == 'klippy:connect':
                callback()


def load_module(path):
    spec = importlib.util.spec_from_file_location('retained_gcode_macro', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_sections(path):
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.read(path, encoding='utf-8')
    return {name: dict(parser.items(name)) for name in parser.sections()}

# This bounded model makes public fixture tests runnable without vendor source.
# The private verifier supplies the retained module to the same Host instead.
class StatusView:
    def __init__(self, printer):
        self.printer, self.cache = printer, {}

    def __getitem__(self, key):
        import copy
        if key not in self.cache:
            self.cache[key] = copy.deepcopy(self.printer.objects[key].get_status(0.))
        return self.cache[key]

    def __getattr__(self, key):
        return self[key]


class ModelTemplate:
    def __init__(self, host, env, source):
        self.host, self.template = host, env.from_string(source)

    def create_template_context(self):
        return {'printer': StatusView(self.host), 'action_raise_error': self.raise_error}

    @staticmethod
    def raise_error(message):
        raise RuntimeError(message)

    def run_gcode_from_command(self, context):
        self.host.gcode.run_script_from_command(self.template.render(context))


class ModelPrinterGCodeMacro:
    def __init__(self, config):
        import jinja2
        self.host = config.get_printer()
        self.env = jinja2.Environment('{%', '%}', '{', '}')

    def load_template(self, config, option):
        return ModelTemplate(self.host, self.env, config.get(option))


class ModelGCodeMacro:
    def __init__(self, config):
        import ast
        self.host = config.get_printer()
        self.alias = config.get_name().split()[1].upper()
        self.template = self.host.objects['gcode_macro'].load_template(config, 'gcode')
        self.rename = config.get('rename_existing')
        self.variables = {key[9:]: ast.literal_eval(config.get(key))
                          for key in config.get_prefix_options('variable_')}
        self.host.gcode.register_mux_command('SET_GCODE_VARIABLE', 'MACRO', self.alias, self.set_variable)
        if self.rename:
            self.host.register_event_handler('klippy:connect', self.connect)
        else:
            self.host.gcode.register_command(self.alias, self.cmd)

    def connect(self):
        old = self.host.gcode.register_command(self.alias, None)
        if old is None:
            raise RuntimeError('missing base command')
        self.host.gcode.register_command(self.rename, old)
        self.host.gcode.register_command(self.alias, self.cmd)

    def get_status(self, eventtime):
        return self.variables

    def set_variable(self, command):
        import ast
        key = command.get('VARIABLE')
        if key not in self.variables:
            raise RuntimeError('unknown variable')
        self.variables = dict(self.variables, **{key: ast.literal_eval(command.get('VALUE'))})

    def cmd(self, command):
        context = dict(self.variables)
        context.update(self.template.create_template_context())
        context.update(params=command.get_command_parameters(), rawparams=command.raw)
        self.template.run_gcode_from_command(context)


class Model:
    PrinterGCodeMacro = ModelPrinterGCodeMacro
    GCodeMacro = ModelGCodeMacro
