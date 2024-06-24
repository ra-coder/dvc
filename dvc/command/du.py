import argparse
import logging

from dvc.command import completion
from dvc.command.base import CmdBase, append_doc_link
from dvc.exceptions import DvcException

logger = logging.getLogger(__name__)


class CmdDiskUsage(CmdBase):
    def run(self):
        try:
            logger.info("\n".join(["du stab works"]))
            # self.repo.move(self.args.src, self.args.dst)
        except DvcException:
            msg = "failed du"
            logger.exception(msg)
            return 1
        return 0


def add_parser(subparsers, parent_parser):
    DU_HELP = "TODO"
    DU_DESCRIPTION = (
        "TODO"
    )

    du_parser = subparsers.add_parser(
        "du",
        parents=[parent_parser],
        description=append_doc_link(DU_DESCRIPTION, "move"),
        help=DU_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # du_parser.add_argument(
    #     "src", help="Source path to a data file or directory.",
    # ).complete = completion.FILE
    # du_parser.add_argument(
    #     "dst", help="Destination path.",
    # ).complete = completion.FILE
    du_parser.set_defaults(func=CmdDiskUsage)
