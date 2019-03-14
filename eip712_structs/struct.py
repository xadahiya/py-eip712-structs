import re
from collections import OrderedDict, defaultdict
from typing import List, Tuple

from eth_utils.crypto import keccak

from eip712_structs.types import Array, EIP712Type, from_solidity_type


class OrderedAttributesMeta(type):
    """Metaclass to ensure struct attribute order is preserved.
    """
    @classmethod
    def __prepare__(mcs, name, bases):
        return OrderedDict()


class _EIP712StructTypeHelper(EIP712Type, metaclass=OrderedAttributesMeta):
    """Helper class to map the more complex struct type to the basic type interface.
    """

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.type_name = cls.__name__


class EIP712Struct(_EIP712StructTypeHelper):
    """A representation of an EIP712 struct. Subclass it to use it.

    Example:
        from eip712_structs import EIP712Struct, String

        class MyStruct(EIP712Struct):
            some_param = String()

        struct_instance = MyStruct(some_param='some_value')
    """
    def __init__(self, **kwargs):
        super(EIP712Struct, self).__init__(self.type_name)
        members = self.get_members()
        self.values = dict()
        for name, typ in members:
            value = kwargs.get(name)
            if isinstance(value, dict):
                value = typ(**value)
            self.values[name] = value

    def encode_value(self, value=None):
        """Returns the struct's encoded value.

        A struct's encoded value is a concatenation of the bytes32 representation of each member of the struct.
        Order is preserved.

        :param value: This parameter is not used for structs.
        """
        encoded_values = [typ.encode_value(self.values[name]) for name, typ in self.get_members()]
        return b''.join(encoded_values)

    def get_data_value(self, name):
        """Get the value of the given struct parameter.
        """
        return self.values.get(name)

    def set_data_value(self, name, value):
        """Set the value of the given struct parameter.
        """
        if name in self.values:
            self.values[name] = value

    def data_dict(self):
        """Provide the entire data dictionary representing the struct.

        Nested structs instances are also converted to dict form.
        """
        result = dict()
        for k, v in self.values.items():
            if isinstance(v, EIP712Struct):
                result[k] = v.data_dict()
            else:
                result[k] = v
        return result

    @classmethod
    def _encode_type(cls, resolve_references: bool) -> str:
        member_sigs = [f'{typ.type_name} {name}' for name, typ in cls.get_members()]
        struct_sig = f'{cls.type_name}({",".join(member_sigs)})'

        if resolve_references:
            reference_structs = set()
            cls._gather_reference_structs(reference_structs)
            sorted_structs = sorted(list(s for s in reference_structs if s != cls), key=lambda s: s.type_name)
            for struct in sorted_structs:
                struct_sig += struct._encode_type(resolve_references=False)
        return struct_sig

    @classmethod
    def _gather_reference_structs(cls, struct_set):
        structs = [m[1] for m in cls.get_members() if isinstance(m[1], type) and issubclass(m[1], EIP712Struct)]
        for struct in structs:
            if struct not in struct_set:
                struct_set.add(struct)
                struct._gather_reference_structs(struct_set)

    @classmethod
    def encode_type(cls):
        """Get the encoded type signature of the struct.

        Nested structs are also encoded, and appended in alphabetical order.
        """
        return cls._encode_type(True)

    @classmethod
    def type_hash(cls):
        """Get the keccak hash of the struct's encoded type."""
        return keccak(text=cls.encode_type())

    def hash_struct(self):
        """The hash of the struct.

        hash_struct => keccak(type_hash || encode_data)
        """
        return keccak(b''.join([self.type_hash(), self.encode_data()]))

    @classmethod
    def get_members(cls) -> List[Tuple[str, EIP712Type]]:
        """A list of tuples of supported parameters.

        Each tuple is (<parameter_name>, <parameter_type>). The list's order is determined by definition order.
        """
        members = [m for m in cls.__dict__.items() if isinstance(m[1], EIP712Type)
                   or (isinstance(m[1], type) and issubclass(m[1], EIP712Struct))]
        return members


def struct_to_dict(primary_struct: EIP712Struct, domain: EIP712Struct):
    """Convert a struct into a dictionary suitable for messaging.

    Dictionary is of the form:
        {
            'primaryType': Name of the primary type,
            'types': Definition of each included struct type (including the domain type)
            'domain': Values for the domain struct,
            'message': Values for the message struct,
        }

    The hash is constructed as:
    `` b'\\x19\\x01' + domain_type_hash + struct_type_hash ``

    :returns: A tuple in the form of: (message_dict, encoded_message_hash>)
    """
    structs = {domain, primary_struct}
    primary_struct._gather_reference_structs(structs)

    # Build type dictionary
    types = dict()
    for struct in structs:
        members_json = [{
            'name': m[0],
            'type': m[1].type_name,
        } for m in struct.get_members()]
        types[struct.type_name] = members_json

    result = {
        'primaryType': primary_struct.type_name,
        'types': types,
        'domain': domain.data_dict(),
        'message': primary_struct.data_dict(),
    }

    typed_data_hash = keccak(b'\x19\x01' + domain.type_hash() + primary_struct.type_hash())

    return result, typed_data_hash


def struct_from_dict(message_dict):
    """Return the EIP712Struct object of the message and domain structs.

    :returns: A tuple in the form of: (<primary struct>, <domain struct>)
    """
    structs = dict()
    unfulfilled_struct_params = defaultdict(list)

    for type_name in message_dict['types']:
        # Dynamically construct struct class from dict representation
        StructFromJSON = type(type_name, (EIP712Struct,), {})

        for member in message_dict['types'][type_name]:
            # Either a basic solidity type is set, or None if referring to a reference struct (we'll fill that later)
            member_name = member['name']
            member_sol_type = from_solidity_type(member['type'])
            setattr(StructFromJSON, member_name, member_sol_type)
            if member_sol_type is None:
                # Track the refs we'll need to set later.
                unfulfilled_struct_params[type_name].append((member_name, member['type']))

        structs[type_name] = StructFromJSON

    # Now that custom structs have been parsed, pass through again to set the references
    for struct_name, unfulfilled_member_names in unfulfilled_struct_params.items():
        regex_pattern = r'([a-zA-Z0-9_]+)(\[(\d+)?\])?'

        struct_class = structs[struct_name]
        for name, type_name in unfulfilled_member_names:
            match = re.match(regex_pattern, type_name)
            base_type_name = match.group(1)
            ref_struct = structs[base_type_name]
            if match.group(2):
                # The type is an array of the struct
                arr_len = match.group(3) or 0  # length of 0 means the array is dynamically sized
                setattr(struct_class, name, Array(ref_struct, arr_len))
            else:
                setattr(struct_class, name, ref_struct)

    primary_struct = structs[message_dict['primaryType']]
    domain_struct = structs['EIP712Domain']

    return primary_struct(**message_dict['message']), domain_struct(**message_dict['domain'])