// Fork-only native regression fixture for OpenROAD #11737.
// Upstream rollback author: ericastor. This fixture is AI-assisted downstream QA.
// This file is injected into TestResizer.cc only in an ephemeral CI checkout.
// It does not change the production policy source.
TEST_F(TestResizer, SetupLegacyPolicyRestoresWorseningMoveOnIterationLimit)
{
  readVerilogAndSetup("TestResizer_SwapPinsFeedthroughModNet_pre.v",
                      /*init_default_sdc=*/true,
                      /*hierarchy=*/false);

  odb::dbInst* target_db = block_->findInst("target");
  ASSERT_NE(target_db, nullptr);
  sta::Instance* target_inst = db_network_->dbToSta(target_db);
  ASSERT_NE(target_inst, nullptr);
  sta::LibertyCell* or2_x1 = sta_->network()->findLibertyCell("OR2_X1");
  sta::LibertyCell* or2_x4 = sta_->network()->findLibertyCell("OR2_X4");
  ASSERT_NE(or2_x1, nullptr);
  ASSERT_NE(or2_x4, nullptr);
  ASSERT_TRUE(resizer_.replaceCell(target_inst, or2_x4));
  resizer_.initBlock();
  sta_->updateTiming(true);

  sta::Pin* out1_pin = findTopPin("out1");
  ASSERT_NE(out1_pin, nullptr);
  sta::Port* out1_port = db_network_->port(out1_pin);
  ASSERT_NE(out1_port, nullptr);
  sta::Sdc* sdc = sta_->cmdMode()->sdc();
  sta::Clock* clk = sdc->findClock("clk");
  ASSERT_NE(clk, nullptr);

  const float ext_cap = sta_->units()->capacitanceUnit()->userToSta(50.0f);
  sta_->setPortExtPinCap(out1_port,
                         sta::RiseFallBoth::riseFall(),
                         sta::MinMaxAll::all(),
                         ext_cap,
                         sdc);
  sta_->updateTiming(true);
  sta::Vertex* endpoint = sta_->graph()->pinLoadVertex(out1_pin);
  ASSERT_NE(endpoint, nullptr);
  const sta::Slack base_slack = sta_->slack(endpoint, sta::MinMax::max());
  sta_->setOutputDelay(out1_pin,
                       sta::RiseFallBoth::riseFall(),
                       clk,
                       sta::RiseFall::rise(),
                       nullptr,
                       false,
                       false,
                       sta::MinMaxAll::all(),
                       /*add=*/false,
                       base_slack + staTime(0.10f),
                       sdc);
  sta_->updateTiming(true);

  const odb::dbMaster* saved_master = target_db->getMaster();
  MoveCommitter committer(resizer_);
  RepairSetupContext setup_context(resizer_);
  OptimizerRunConfig config;
  config.max_iterations = 1;
  config.setup_slack_margin = 0.0f;
  TestSetupLegacyPolicy policy(resizer_, committer, setup_context, config);
  ASSERT_TRUE(policy.start());

  TestSetupLegacyPolicy::EndpointRepairState endpoint_state;
  endpoint_state.end = endpoint;
  policy.refreshEndpointSlacks(endpoint_state);
  ASSERT_LT(endpoint_state.end_slack, 0.0f);
  endpoint_state.prev_end_slack = endpoint_state.end_slack;
  endpoint_state.prev_worst_slack = endpoint_state.worst_slack;
  committer.beginJournal();
  endpoint_state.journal_open = true;

  TestSetupLegacyPolicy::MainRepairState main_state;
  main_state.end_index = 2;
  main_state.max_end_count = 2;
  main_state.num_viols = 2;

  int calls = 0;
  policy.setRepairPathStep([&]() {
    ++calls;
    return resizer_.replaceCell(target_inst, or2_x1);
  });
  {
    est::IncrementalParasiticsGuard guard(&ep_);
    policy.repairEndpoint(endpoint_state, main_state);
  }

  ASSERT_EQ(calls, 1);
  ASSERT_EQ(main_state.opto_iteration, 1);
  ASSERT_EQ(endpoint_state.pass, 2)
      << "Fixture must reach the iteration guard after mutation";
  ASSERT_GT(endpoint_state.decreasing_slack_passes, 0)
      << "Fixture must exercise non-improving endpoint rollback";
  EXPECT_FALSE(endpoint_state.journal_open);
  odb::dbInst* restored_target = block_->findInst("target");
  ASSERT_NE(restored_target, nullptr);
  EXPECT_EQ(restored_target->getMaster(), saved_master);
}
