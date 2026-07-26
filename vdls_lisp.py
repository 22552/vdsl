"""Finite, pure compile-time Lisp evaluator and structural expander for VDLS."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Any, Callable


@dataclass(frozen=True)
class Limits:
    max_steps: int = 100_000
    max_iterations: int = 10_000
    max_nodes: int = 100_000
    max_depth: int = 128


@dataclass(frozen=True)
class Scalar:
    value: Fraction
    dimension: str | None = None


@dataclass(frozen=True)
class Closure:
    parameters: tuple[str,...]
    body: Any
    environment: "Environment"
    name: str = "<lambda>"
    parameter_types: tuple[str | None,...] = ()
    return_type: str | None = None


class Environment:
    def __init__(self, parent: "Environment | None"=None):
        self.parent=parent
        self.values: dict[str,Any]={}

    def define(self, name: str, value: Any, diagnostic: Callable[...,Exception]):
        if name in self.values:
            raise diagnostic(
                "VDLS-NAME-002",f"duplicate value definition `{name}`")
        self.values[name]=value

    def lookup(self, name: str) -> Any:
        if name in self.values: return self.values[name]
        if self.parent is not None: return self.parent.lookup(name)
        raise KeyError(name)


class LispExpander:
    STRUCTURAL={
        "project","id","output","file","video","audio","asset","generated",
        "solid-color","linear-gradient","radial-gradient","checkerboard",
        "tone","silence","scene","duration","layer","text","shape","group",
        "stack","grid","subtitles","asset-ref","position","anchor","font",
        "family","size","weight","fill","stroke","shadow","box","width",
        "height","wrap","overflow","align","transform","scale","rotation",
        "opacity","blend","filter","filters","animate","from","to","easing",
        "keyframes","start","gap","padding","columns","style","language",
        "sidecar","burn-in","timeline","marker","at","label","metadata",
        "sample-rate","channels","fps","build-options","seed","color-management",
        "working-space","primaries","transfer","matrix","range","preset",
        "length",
        "scene-ref","use-scene","trim","speed","gain","pan","fade-in",
        "fade-out","typewriter","reveal-lines","highlight-words",
        "text-fade-in","integrity","sha256","color","tone-map",
    }

    def __init__(self, symbol_type: type, diagnostic: Callable[...,Exception],
                 limits: Limits=Limits()):
        self.Symbol=symbol_type
        self.Diagnostic=diagnostic
        self.limits=limits
        self.steps=0; self.iterations=0; self.nodes=0
        self.global_env=Environment()
        self.components: dict[str,dict[str,Any]]={}

    def tick(self, depth: int=0, nodes: int=0) -> None:
        self.steps+=1; self.nodes+=nodes
        if (depth>self.limits.max_depth
                or self.steps>self.limits.max_steps
                or self.nodes>self.limits.max_nodes):
            raise self.Diagnostic(
                "VDLS-LISP-030","compile-time expansion limit exceeded")

    def atom(self, value: Any) -> Any:
        if not isinstance(value,self.Symbol): return value
        text=str(value)
        if text in {"true","#t"}: return True
        if text in {"false","#f"}: return False
        try:
            if "/" in text and all(part.lstrip("+-").isdigit()
                                   for part in text.split("/",1)):
                return Scalar(Fraction(text),None)
            if text.lstrip("+-").replace(".","",1).isdigit():
                return Scalar(Fraction(text),None)
        except (ValueError,ZeroDivisionError):
            pass
        suffixes=(
            ("ms","time",Fraction(1,1000)),("us","time",Fraction(1,1_000_000)),
            ("ns","time",Fraction(1,1_000_000_000)),("s","time",Fraction(1)),
            ("px","length",Fraction(1)),("%","ratio",Fraction(1,100)),
            ("pct","ratio",Fraction(1,100)),("deg","angle",
             Fraction(str(math.pi))/180),("rad","angle",Fraction(1)),
            ("Hz","frequency",Fraction(1)),("hz","frequency",Fraction(1)),
            ("kHz","frequency",Fraction(1000)),("khz","frequency",Fraction(1000)),
            ("dB","gain-db",Fraction(1)),("db","gain-db",Fraction(1)),
        )
        for suffix,dimension,factor in suffixes:
            if text.endswith(suffix):
                raw=text[:-len(suffix)]
                try: return Scalar(Fraction(raw)*factor,dimension)
                except (ValueError,ZeroDivisionError): break
        try: return self.global_env.lookup(text)
        except KeyError: return value

    def emit(self, value: Any) -> Any:
        if isinstance(value,Scalar):
            number=value.value
            raw=(str(number.numerator) if number.denominator==1
                 else f"{number.numerator}/{number.denominator}")
            suffix={
                None:"","time":"s","length":"px","ratio":"pct",
                "angle":"rad","frequency":"Hz","gain-db":"dB",
            }[value.dimension]
            if value.dimension=="ratio":
                number*=100
                raw=(str(number.numerator) if number.denominator==1
                     else f"{number.numerator}/{number.denominator}")
            return self.Symbol(raw+suffix)
        if isinstance(value,bool):
            return self.Symbol("true" if value else "false")
        if isinstance(value,list):
            return [self.emit(item) for item in value]
        return value

    def scalar(self, value: Any, operation: str) -> Scalar:
        if not isinstance(value,Scalar):
            raise self.Diagnostic(
                "VDLS-LISP-002",f"`{operation}` requires numeric operands")
        return value

    def numeric(self, operator: str, arguments: list[Any]) -> Any:
        values=[self.scalar(value,operator) for value in arguments]
        if operator in {"+","-","min","max"}:
            dimensions={value.dimension for value in values}
            if len(dimensions)!=1:
                raise self.Diagnostic(
                    "VDLS-TYPE-004",
                    f"`{operator}` received incompatible dimensions")
            if operator=="+":
                result=sum((v.value for v in values),Fraction(0))
            elif operator=="-":
                result=(values[0].value-values[1].value if len(values)==2
                        else -values[0].value)
            elif operator=="min": result=min(v.value for v in values)
            else: result=max(v.value for v in values)
            return Scalar(result,values[0].dimension)
        if operator=="*":
            dimensional=[value for value in values if value.dimension]
            if len(dimensional)>1:
                raise self.Diagnostic(
                    "VDLS-TYPE-004","multiplication has incompatible dimensions")
            result=Fraction(1)
            for value in values: result*=value.value
            return Scalar(
                result,dimensional[0].dimension if dimensional else None)
        if operator=="/":
            if len(values)!=2 or values[1].value==0:
                raise self.Diagnostic(
                    "VDLS-LISP-020","invalid division")
            dimension=(None if values[0].dimension==values[1].dimension
                       else values[0].dimension)
            if values[1].dimension and values[0].dimension!=values[1].dimension:
                raise self.Diagnostic(
                    "VDLS-TYPE-004","division has incompatible dimensions")
            return Scalar(values[0].value/values[1].value,dimension)
        raise AssertionError(operator)

    def call(self, function: Any, arguments: list[Any], depth: int) -> Any:
        if not isinstance(function,Closure):
            raise self.Diagnostic(
                "VDLS-LISP-002","attempted to call a non-function")
        if len(arguments)!=len(function.parameters):
            raise self.Diagnostic(
                "VDLS-LISP-003",
                f"`{function.name}` expected {len(function.parameters)} "
                f"arguments, received {len(arguments)}")
        env=Environment(function.environment)
        for index,(name,value) in enumerate(
                zip(function.parameters,arguments)):
            expected=(function.parameter_types[index]
                      if function.parameter_types else None)
            if not self.matches_type(value,expected):
                raise self.Diagnostic(
                    "VDLS-LISP-002",
                    f"`{function.name}` argument `{name}` does not "
                    f"match {expected}")
            env.define(name,value,self.Diagnostic)
        result=self.evaluate(function.body,env,depth+1)
        if not self.matches_type(result,function.return_type):
            raise self.Diagnostic(
                "VDLS-LISP-002",
                f"`{function.name}` result does not match "
                f"{function.return_type}")
        return result

    def evaluate(self, expression: Any, env: Environment, depth: int=0) -> Any:
        self.tick(depth)
        if isinstance(expression,self.Symbol):
            value=self.atom(expression)
            if value is expression:
                try: return env.lookup(str(expression))
                except KeyError:
                    raise self.Diagnostic(
                        "VDLS-NAME-001",
                        f"undefined identifier `{expression}`")
            return value
        if not isinstance(expression,list): return expression
        if not expression: return []
        if isinstance(expression[0],list):
            function=self.evaluate(expression[0],env,depth+1)
            return self.call(
                function,
                [self.evaluate(item,env,depth+1)
                 for item in expression[1:]],depth)
        head=str(expression[0])
        if head=="lambda":
            if (len(expression) not in {3,5}
                    or not isinstance(expression[1],list)
                    or (len(expression)==5
                        and str(expression[2])!=":")):
                raise self.Diagnostic(
                    "VDLS-LISP-001","malformed lambda")
            parameters=tuple(self.parameter_name(item)
                             for item in expression[1])
            parameter_types=tuple(
                self.parameter_type(item) for item in expression[1])
            if len(parameters)!=len(set(parameters)):
                raise self.Diagnostic(
                    "VDLS-LISP-001","duplicate lambda parameter")
            return Closure(
                parameters,expression[-1],env,"<lambda>",
                parameter_types,
                str(expression[3]) if len(expression)==5 else None)
        if head in {"let","let*"}:
            if len(expression)!=3 or not isinstance(expression[1],list):
                raise self.Diagnostic(
                    "VDLS-LISP-010",f"malformed {head}")
            local=Environment(env)
            seen=set()
            for binding in expression[1]:
                if not isinstance(binding,list) or len(binding)!=2:
                    raise self.Diagnostic(
                        "VDLS-LISP-010","malformed local binding")
                name=self.parameter_name(binding[0])
                if name in seen:
                    raise self.Diagnostic(
                        "VDLS-LISP-010",f"duplicate local binding `{name}`")
                seen.add(name)
                value=self.evaluate(
                    binding[1],local if head=="let*" else env,depth+1)
                local.define(name,value,self.Diagnostic)
            return self.evaluate(expression[2],local,depth+1)
        if head=="if":
            if len(expression)!=4:
                raise self.Diagnostic(
                    "VDLS-LISP-003","if requires three operands")
            condition=self.evaluate(expression[1],env,depth+1)
            return self.evaluate(
                expression[2] if self.truth(condition) else expression[3],
                env,depth+1)
        if head=="cond":
            for index,clause in enumerate(expression[1:]):
                if not isinstance(clause,list) or len(clause)!=2:
                    raise self.Diagnostic(
                        "VDLS-LISP-010","malformed cond clause")
                if str(clause[0])=="else":
                    if index!=len(expression[1:])-1:
                        raise self.Diagnostic(
                            "VDLS-LISP-010","cond else must be last")
                    return self.evaluate(clause[1],env,depth+1)
                if self.truth(self.evaluate(clause[0],env,depth+1)):
                    return self.evaluate(clause[1],env,depth+1)
            return None
        if head in {"and","or"}:
            result=True if head=="and" else False
            for item in expression[1:]:
                result=self.evaluate(item,env,depth+1)
                if head=="and" and not self.truth(result): return result
                if head=="or" and self.truth(result): return result
            return result
        if head=="for/list" or head=="for*/list":
            return self.evaluate_for(
                expression,env,depth,cartesian=head=="for*/list")
        if head=="for/fold":
            return self.evaluate_for_fold(expression,env,depth)
        if head in {"list","cons","first","rest","length","append","range",
                    "map","list-filter","foldl","foldr","flat-map","take",
                    "drop","enumerate"}:
            return self.collection(head,expression[1:],env,depth)
        if head in {"+","-","*","/","min","max"}:
            return self.numeric(
                head,[self.evaluate(item,env,depth+1)
                      for item in expression[1:]])
        if head in {"=","!=","<","<=",">",">="}:
            values=[self.evaluate(item,env,depth+1)
                    for item in expression[1:]]
            if len(values)!=2:
                raise self.Diagnostic(
                    "VDLS-LISP-003",f"{head} requires two operands")
            left,right=values
            if isinstance(left,Scalar) and isinstance(right,Scalar):
                if left.dimension!=right.dimension:
                    raise self.Diagnostic(
                        "VDLS-TYPE-004","comparison dimensions differ")
                left,right=left.value,right.value
            return {
                "=":left==right,"!=":left!=right,"<":left<right,
                "<=":left<=right,">":left>right,">=":left>=right,
            }[head]
        if head=="not":
            if len(expression)!=2:
                raise self.Diagnostic(
                    "VDLS-LISP-003","not requires one operand")
            return not self.truth(self.evaluate(expression[1],env,depth+1))
        try: function=env.lookup(head)
        except KeyError:
            try: function=self.global_env.lookup(head)
            except KeyError:
                raise self.Diagnostic(
                    "VDLS-NAME-001",f"undefined function `{head}`")
        return self.call(
            function,[self.evaluate(item,env,depth+1)
                      for item in expression[1:]],depth)

    def truth(self, value: Any) -> bool:
        return value is not False

    def parameter_name(self, value: Any) -> str:
        if isinstance(value,list):
            if len(value)>=1: return str(value[0])
        if isinstance(value,self.Symbol): return str(value)
        raise self.Diagnostic(
            "VDLS-LISP-001","invalid parameter")

    def parameter_type(self, value: Any) -> str | None:
        if (isinstance(value,list) and len(value)>=3
                and str(value[1])==":"):
            return str(value[2])
        return None

    def collection(self, head: str, arguments: list[Any],
                   env: Environment, depth: int) -> Any:
        values=[self.evaluate(item,env,depth+1) for item in arguments]
        if head=="list": return values
        if head=="cons":
            if len(values)!=2 or not isinstance(values[1],list):
                raise self.Diagnostic("VDLS-LISP-020","invalid cons")
            return [values[0],*values[1]]
        if head in {"first","rest","length"}:
            if len(values)!=1 or not isinstance(values[0],list):
                raise self.Diagnostic(
                    "VDLS-LISP-020",f"invalid {head}")
            if head!="length" and not values[0]:
                raise self.Diagnostic(
                    "VDLS-LISP-020",f"{head} of empty list")
            if head=="first": return values[0][0]
            if head=="rest": return values[0][1:]
            return Scalar(Fraction(len(values[0])),None)
        if head=="append":
            if not all(isinstance(value,list) for value in values):
                raise self.Diagnostic("VDLS-LISP-020","append requires lists")
            return [item for value in values for item in value]
        if head=="range":
            scalars=[self.scalar(value,"range") for value in values]
            if not 1<=len(scalars)<=3 or any(
                    item.dimension for item in scalars):
                raise self.Diagnostic("VDLS-LISP-020","invalid range")
            if len(scalars)==1: start,end,step=0,scalars[0].value,1
            elif len(scalars)==2:
                start,end,step=scalars[0].value,scalars[1].value,1
            else:
                start,end,step=(item.value for item in scalars)
            if any(item.denominator!=1 for item in (start,end,step)) or step==0:
                raise self.Diagnostic(
                    "VDLS-LISP-020","range requires integer bounds")
            result=[Scalar(Fraction(i),None)
                    for i in range(int(start),int(end),int(step))]
            self.iterations+=len(result)
            self.check_iterations()
            return result
        if head in {"map","list-filter","foldl","foldr","flat-map"}:
            if not values or not isinstance(values[0],Closure):
                raise self.Diagnostic(
                    "VDLS-LISP-002",f"{head} requires a function")
            function=values[0]
            lists=values[1:] if head=="map" else values[-1:]
            if not lists or not all(isinstance(value,list) for value in lists):
                raise self.Diagnostic(
                    "VDLS-LISP-020",f"{head} requires list operands")
            if head=="map":
                lengths={len(value) for value in lists}
                if len(lengths)!=1:
                    raise self.Diagnostic(
                        "VDLS-LISP-021","parallel sequence lengths differ")
                return [self.call(function,list(items),depth+1)
                        for items in zip(*lists)]
            if head=="list-filter":
                return [item for item in lists[0]
                        if self.truth(self.call(function,[item],depth+1))]
            if head=="flat-map":
                result=[]
                for item in lists[0]:
                    value=self.call(function,[item],depth+1)
                    if not isinstance(value,list):
                        raise self.Diagnostic(
                            "VDLS-LISP-020","flat-map function must return list")
                    result.extend(value)
                return result
            if len(values)!=3:
                raise self.Diagnostic(
                    "VDLS-LISP-003",f"{head} requires function, initial, list")
            accumulator=values[1]
            sequence=lists[0] if head=="foldl" else reversed(lists[0])
            for item in sequence:
                accumulator=self.call(
                    function,[accumulator,item],depth+1)
            return accumulator
        if head in {"take","drop"}:
            if (len(values)!=2 or not isinstance(values[0],list)
                    or not isinstance(values[1],Scalar)
                    or values[1].dimension
                    or values[1].value.denominator!=1):
                raise self.Diagnostic(
                    "VDLS-LISP-020",f"invalid {head}")
            count=int(values[1].value)
            if count<0 or count>len(values[0]):
                raise self.Diagnostic(
                    "VDLS-LISP-020",f"{head} count out of range")
            return values[0][:count] if head=="take" else values[0][count:]
        if head=="enumerate":
            if len(values)!=1 or not isinstance(values[0],list):
                raise self.Diagnostic("VDLS-LISP-020","invalid enumerate")
            return [[Scalar(Fraction(i),None),item]
                    for i,item in enumerate(values[0])]
        raise AssertionError(head)

    def evaluate_for(self, expression: list[Any], env: Environment,
                     depth: int, cartesian: bool) -> list[Any]:
        if len(expression)!=3 or not isinstance(expression[1],list):
            raise self.Diagnostic("VDLS-LISP-010","malformed for/list")
        bindings=[]
        for binding in expression[1]:
            if not isinstance(binding,list) or len(binding)!=2:
                raise self.Diagnostic("VDLS-LISP-010","malformed for binding")
            sequence=self.evaluate(binding[1],env,depth+1)
            if not isinstance(sequence,list):
                raise self.Diagnostic(
                    "VDLS-LISP-020","for sequence must be a list")
            bindings.append((self.parameter_name(binding[0]),sequence))
        if not cartesian:
            lengths={len(sequence) for _,sequence in bindings}
            if len(lengths)>1:
                raise self.Diagnostic(
                    "VDLS-LISP-021","parallel sequence lengths differ")
            rows=zip(*(sequence for _,sequence in bindings))
        else:
            rows=[()]
            for _,sequence in bindings:
                rows=[(*row,value) for row in rows for value in sequence]
        result=[]
        for row in rows:
            self.iterations+=1; self.check_iterations()
            local=Environment(env)
            for (name,_),value in zip(bindings,row):
                local.define(name,value,self.Diagnostic)
            result.append(self.evaluate(expression[2],local,depth+1))
        return result

    def evaluate_for_fold(self, expression: list[Any], env: Environment,
                          depth: int) -> Any:
        if (len(expression)!=4 or not isinstance(expression[1],list)
                or len(expression[1])!=1
                or not isinstance(expression[2],list)):
            raise self.Diagnostic("VDLS-LISP-010","malformed for/fold")
        accumulator_binding=expression[1][0]
        if not isinstance(accumulator_binding,list) or len(
                accumulator_binding)!=2:
            raise self.Diagnostic("VDLS-LISP-010","malformed fold binding")
        name=self.parameter_name(accumulator_binding[0])
        accumulator=self.evaluate(accumulator_binding[1],env,depth+1)
        fake=[self.Symbol("for/list"),expression[2],expression[3]]
        # Evaluate each body with the current accumulator visible.
        bindings=[]
        for binding in fake[1]:
            sequence=self.evaluate(binding[1],env,depth+1)
            if not isinstance(sequence,list):
                raise self.Diagnostic(
                    "VDLS-LISP-020","for/fold sequence must be a list")
            bindings.append((self.parameter_name(binding[0]),sequence))
        lengths={len(sequence) for _,sequence in bindings}
        if len(lengths)>1:
            raise self.Diagnostic(
                "VDLS-LISP-021","parallel sequence lengths differ")
        for row in zip(*(sequence for _,sequence in bindings)):
            self.iterations+=1; self.check_iterations()
            local=Environment(env)
            local.define(name,accumulator,self.Diagnostic)
            for (binding_name,_),value in zip(bindings,row):
                local.define(binding_name,value,self.Diagnostic)
            accumulator=self.evaluate(expression[3],local,depth+1)
        return accumulator

    def check_iterations(self) -> None:
        if self.iterations>self.limits.max_iterations:
            raise self.Diagnostic(
                "VDLS-LISP-030","compile-time iteration limit exceeded")

    def expand_structure(self, value: Any, env: Environment,
                         depth: int=0) -> Any:
        self.tick(depth,nodes=1 if isinstance(value,list) else 0)
        if isinstance(value,self.Symbol):
            try: return self.emit(env.lookup(str(value)))
            except KeyError:
                try: return self.emit(self.global_env.lookup(str(value)))
                except KeyError: return value
        if not isinstance(value,list): return value
        if not value: return []
        head=str(value[0])
        if head=="slot-ref":
            if len(value)!=2:
                raise self.Diagnostic(
                    "VDLS-LISP-042","slot-ref requires one slot name")
            try: return self.emit(env.lookup(str(value[1])))
            except KeyError:
                raise self.Diagnostic(
                    "VDLS-LISP-042",f"unknown slot `{value[1]}`")
        if head in self.components:
            return self.expand_component(value,env,depth)
        if head not in self.STRUCTURAL and head in {"for/list","for*/list"}:
            return self.expand_for_structure(
                value,env,depth,head=="for*/list")
        if head not in self.STRUCTURAL and head in {"for/fold","let","let*","if",
                    "cond","map","list-filter","foldl","foldr","flat-map",
                    "list","cons","first","rest","length","append","range",
                    "take","drop","enumerate","+","-","*","/","min","max",
                    "=","!=","<","<=",">",">=","and","or","not"}:
            try:
                return self.emit(self.evaluate(value,env,depth+1))
            except self.Diagnostic as error:
                # A frame-time variable keeps the expression in the
                # serializable expression language for later evaluation.
                if error.code!="VDLS-NAME-001": raise
        try: function=env.lookup(head)
        except KeyError:
            try: function=self.global_env.lookup(head)
            except KeyError: function=None
        if isinstance(function,Closure):
            arguments=[
                self.evaluate(item,env,depth+1) for item in value[1:]]
            try:
                result=self.call(function,arguments,depth+1)
            except self.Diagnostic as error:
                if error.code!="VDLS-NAME-001": raise
                local=Environment(function.environment)
                for name,argument in zip(function.parameters,arguments):
                    local.define(name,argument,self.Diagnostic)
                return self.expand_structure(function.body,local,depth+1)
            return self.expand_structure(self.emit(result),env,depth+1)
        output=[value[0]]
        for item in value[1:]:
            expanded=self.expand_structure(item,env,depth+1)
            if (isinstance(expanded,list) and expanded
                    and all(isinstance(child,list) for child in expanded)
                    and head in {"project","scene","group","stack","grid"}):
                output.extend(expanded)
            else:
                output.append(expanded)
        return output

    def expand_for_structure(
        self, expression: list[Any], env: Environment, depth: int,
        cartesian: bool,
    ) -> list[Any]:
        if len(expression)!=3 or not isinstance(expression[1],list):
            raise self.Diagnostic("VDLS-LISP-010","malformed structural for")
        bindings=[]
        for binding in expression[1]:
            if not isinstance(binding,list) or len(binding)!=2:
                raise self.Diagnostic(
                    "VDLS-LISP-010","malformed structural for binding")
            sequence=self.evaluate(binding[1],env,depth+1)
            if not isinstance(sequence,list):
                raise self.Diagnostic(
                    "VDLS-LISP-020","for sequence must be a finite list")
            bindings.append((self.parameter_name(binding[0]),sequence))
        if cartesian:
            rows=[()]
            for _,sequence in bindings:
                rows=[(*row,item) for row in rows for item in sequence]
        else:
            lengths={len(sequence) for _,sequence in bindings}
            if len(lengths)>1:
                raise self.Diagnostic(
                    "VDLS-LISP-021","parallel sequence lengths differ")
            rows=list(zip(*(sequence for _,sequence in bindings)))
        result=[]
        for row in rows:
            self.iterations+=1; self.check_iterations()
            local=Environment(env)
            for (name,_),item in zip(bindings,row):
                local.define(name,item,self.Diagnostic)
            expanded=self.expand_structure(expression[2],local,depth+1)
            if (isinstance(expanded,list) and expanded
                    and all(isinstance(item,list) for item in expanded)):
                result.extend(expanded)
            else:
                result.append(expanded)
        return result

    def define_top(self, form: list[Any]) -> None:
        if len(form)<3:
            raise self.Diagnostic("VDLS-LISP-001","malformed define")
        signature=form[1]
        if isinstance(signature,list):
            if not signature:
                raise self.Diagnostic("VDLS-LISP-001","empty function signature")
            name=str(signature[0])
            parameters=tuple(self.parameter_name(item)
                             for item in signature[1:])
            parameter_types=tuple(
                self.parameter_type(item) for item in signature[1:])
            body=form[-1]
            return_type=(
                str(form[-2])
                if len(form)>=5 and str(form[-3])==":" else None)
            closure=Closure(
                parameters,body,self.global_env,name,
                parameter_types,return_type)
            self.global_env.define(name,closure,self.Diagnostic)
        else:
            name=self.parameter_name(signature)
            value=self.evaluate(form[-1],self.global_env)
            self.global_env.define(name,value,self.Diagnostic)

    def define_component(self, form: list[Any]) -> None:
        if len(form)<4 or not isinstance(form[2],list):
            raise self.Diagnostic(
                "VDLS-LISP-043","malformed component definition")
        name=str(form[1])
        if name in self.components:
            raise self.Diagnostic(
                "VDLS-NAME-002",f"duplicate component `{name}`")
        parameters=[]
        for specification in form[2]:
            if not isinstance(specification,list) or not specification:
                raise self.Diagnostic(
                    "VDLS-LISP-043","malformed component parameter")
            parameter_name=str(specification[0])
            type_name=None; default=None
            if len(specification)>=3 and str(specification[1])==":":
                type_name=str(specification[2])
                if len(specification)>=4: default=specification[3]
            elif len(specification)>=2:
                default=specification[1]
            parameters.append({
                "name":parameter_name,"type":type_name,"default":default})
        slots=[]
        for declaration in form[3:-1]:
            if (not isinstance(declaration,list) or not declaration
                    or declaration[0]!="slot"):
                raise self.Diagnostic(
                    "VDLS-LISP-043","invalid component declaration")
            if len(declaration)!=4 or str(declaration[2])!=":":
                raise self.Diagnostic(
                    "VDLS-LISP-042","slot requires a type")
            slots.append({
                "name":str(declaration[1]),
                "type":str(declaration[3])})
        if len({slot["name"] for slot in slots})!=len(slots):
            raise self.Diagnostic(
                "VDLS-LISP-042","duplicate component slot")
        self.components[name]={
            "parameters":parameters,"slots":slots,"body":form[-1]}

    def matches_type(self, value: Any, type_name: str | None) -> bool:
        if type_name is None: return True
        if type_name in {"String","Color"}:
            return isinstance(value,str) and not isinstance(value,self.Symbol)
        if type_name in {"Int","Integer"}:
            return (isinstance(value,Scalar) and value.dimension is None
                    and value.value.denominator==1)
        if type_name in {"Number","Real"}:
            return isinstance(value,Scalar) and value.dimension is None
        dimensions={
            "Time":"time","Duration":"time","Length":"length",
            "Angle":"angle","Frequency":"frequency","Ratio":"ratio"}
        if type_name in dimensions:
            return (isinstance(value,Scalar)
                    and value.dimension==dimensions[type_name])
        if type_name.startswith("List<"):
            return isinstance(value,list)
        if type_name.startswith("NodeList<"):
            return (isinstance(value,list)
                    and all(isinstance(item,list) for item in value))
        if type_name.startswith("Node<"):
            return isinstance(value,list)
        return True

    def expand_component(self, call: list[Any], env: Environment,
                         depth: int) -> Any:
        definition=self.components[str(call[0])]
        supplied={}
        for argument in call[1:]:
            if not isinstance(argument,list) or len(argument)<2:
                raise self.Diagnostic(
                    "VDLS-LISP-041","component arguments must be named")
            name=str(argument[0])
            if name in supplied:
                raise self.Diagnostic(
                    "VDLS-LISP-041",f"duplicate component argument `{name}`")
            supplied[name]=argument[1] if len(argument)==2 else argument[1:]
        known=({parameter["name"] for parameter in definition["parameters"]}
               |{slot["name"] for slot in definition["slots"]})
        unknown=set(supplied)-known
        if unknown:
            raise self.Diagnostic(
                "VDLS-LISP-041",
                f"unknown component argument `{sorted(unknown)[0]}`")
        local=Environment(env)
        for parameter in definition["parameters"]:
            raw=supplied.get(parameter["name"],parameter["default"])
            if raw is None:
                raise self.Diagnostic(
                    "VDLS-LISP-040",
                    f"missing component argument `{parameter['name']}`")
            try: value=self.evaluate(raw,env,depth+1)
            except Exception:
                # Node/slot syntax remains structural.
                value=raw
            if not self.matches_type(value,parameter["type"]):
                raise self.Diagnostic(
                    "VDLS-LISP-002",
                    f"component argument `{parameter['name']}` does not "
                    f"match {parameter['type']}")
            local.define(parameter["name"],value,self.Diagnostic)
        for slot in definition["slots"]:
            raw=supplied.get(slot["name"])
            if raw is None:
                raise self.Diagnostic(
                    "VDLS-LISP-040",
                    f"missing component slot `{slot['name']}`")
            value=raw if isinstance(raw,list) else [raw]
            if (slot["type"].startswith("NodeList<")
                    and (not isinstance(value,list)
                         or not all(isinstance(item,list)
                                    for item in value))):
                raise self.Diagnostic(
                    "VDLS-LISP-042",
                    f"slot `{slot['name']}` does not match {slot['type']}")
            local.define(slot["name"],value,self.Diagnostic)
        result=self.expand_structure(definition["body"],local,depth+1)
        return result

    def expand(self, forms: list[Any]) -> tuple[list[Any],dict[str,Any]]:
        retained=[]
        for form in forms:
            if isinstance(form,list) and form:
                if str(form[0])=="define":
                    self.define_top(form); continue
                if str(form[0])=="component":
                    self.define_component(form); continue
            retained.append(form)
        expanded=[self.expand_structure(form,self.global_env) for form in retained]
        identity={
            "steps":self.steps,"iterations":self.iterations,
            "nodes":self.nodes,
            "limits":{
                "maxSteps":self.limits.max_steps,
                "maxIterations":self.limits.max_iterations,
                "maxNodes":self.limits.max_nodes,
                "maxDepth":self.limits.max_depth,
            },
            "definitions":sorted(self.global_env.values),
            "components":sorted(self.components),
        }
        return expanded,identity


def expand_lisp(forms: list[Any], symbol_type: type,
                diagnostic: Callable[...,Exception],
                limits: Limits=Limits()) -> tuple[list[Any],dict[str,Any]]:
    return LispExpander(symbol_type,diagnostic,limits).expand(forms)
