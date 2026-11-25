# Hand-written companion to generated.tf: the root provider the generated
# HCL expects. The us-west-2 provider alias is generated.
#
# No subnet_ids variable here: `app` and its us-west-2 copy each set their
# own subnet_ids in multiregion.rig (replace those placeholders, and the
# AMIs, there).

provider "aws" {
  region = "us-east-1" # must match the first region declared in the .rig file
}
